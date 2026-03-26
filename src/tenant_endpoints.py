"""
WellHeard AI — Multi-Tenant & Zoho OAuth API Endpoints

Provides REST endpoints for:
- Company CRUD
- Campaign CRUD
- Lead file upload + import
- Zoho OAuth authorization flow
- Webhook configuration
"""

import os
import uuid
import structlog
from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, UploadFile, File, Form, HTTPException, Query
from fastapi.responses import RedirectResponse, JSONResponse
from pydantic import BaseModel, Field

from .webhooks import ZohoConfig, ZohoCRMClient, PreTransferDispatcher
from .lead_importer import LeadImporter

logger = structlog.get_logger()

router = APIRouter(prefix="/v1", tags=["Tenant Management"])

# ── In-memory stores (replace with DB in production) ────────────────────────
_companies: dict = {}
_campaigns: dict = {}
_leads: dict = {}  # campaign_id → [lead_dicts]
_zoho_configs: dict = {}  # company_id → ZohoConfig

# Shared dispatcher
_dispatcher = PreTransferDispatcher()


# ── Request/Response Models ─────────────────────────────────────────────────


class CompanyCreate(BaseModel):
    name: str
    slug: str = ""
    max_campaigns: int = 5
    max_concurrent_calls: int = 10
    max_daily_calls: int = 500


class CompanyResponse(BaseModel):
    id: str
    name: str
    slug: str
    status: str
    max_campaigns: int
    max_concurrent_calls: int
    max_daily_calls: int
    webhook_url: str = ""
    zoho_configured: bool = False
    created_at: str


class CampaignCreate(BaseModel):
    company_id: str
    name: str
    pipeline_mode: str = "budget"
    transfer_did: str = ""
    transfer_did_backup: str = ""
    cadence_days: list = Field(default_factory=lambda: [1, 2, 4, 7, 14, 21])
    max_attempts: int = 8
    call_window_start: str = "08:00"
    call_window_end: str = "21:00"
    system_prompt: str = ""
    greeting_text: str = ""


class CampaignResponse(BaseModel):
    id: str
    company_id: str
    name: str
    status: str
    pipeline_mode: str
    transfer_did: str
    cadence_days: list
    total_leads: int
    total_calls: int
    qualified_transfers: int
    created_at: str


class WebhookConfig(BaseModel):
    webhook_url: str = ""
    webhook_secret: str = ""


class ZohoSetup(BaseModel):
    client_id: str
    client_secret: str
    redirect_uri: str


# ── Company Endpoints ───────────────────────────────────────────────────────


@router.post("/companies", response_model=CompanyResponse)
async def create_company(body: CompanyCreate):
    """Create a new company (tenant)."""
    company_id = str(uuid.uuid4())
    slug = body.slug or body.name.lower().replace(" ", "-")[:100]

    # Check slug uniqueness
    for c in _companies.values():
        if c["slug"] == slug:
            raise HTTPException(400, f"Slug '{slug}' already exists")

    company = {
        "id": company_id,
        "name": body.name,
        "slug": slug,
        "status": "trial",
        "max_campaigns": body.max_campaigns,
        "max_concurrent_calls": body.max_concurrent_calls,
        "max_daily_calls": body.max_daily_calls,
        "webhook_url": "",
        "webhook_secret": "",
        "zoho_configured": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _companies[company_id] = company

    logger.info("company_created", company_id=company_id, name=body.name)
    return CompanyResponse(**company)


@router.get("/companies/{company_id}", response_model=CompanyResponse)
async def get_company(company_id: str):
    """Get company details."""
    company = _companies.get(company_id)
    if not company:
        raise HTTPException(404, "Company not found")
    return CompanyResponse(**company)


@router.get("/companies", response_model=list)
async def list_companies():
    """List all companies."""
    return [CompanyResponse(**c) for c in _companies.values()]


# ── Campaign Endpoints ──────────────────────────────────────────────────────


@router.post("/campaigns", response_model=CampaignResponse)
async def create_campaign(body: CampaignCreate):
    """Create a new campaign for a company."""
    if body.company_id not in _companies:
        raise HTTPException(404, "Company not found")

    # Check campaign limit
    company_campaigns = [c for c in _campaigns.values() if c["company_id"] == body.company_id]
    max_allowed = _companies[body.company_id]["max_campaigns"]
    if len(company_campaigns) >= max_allowed:
        raise HTTPException(400, f"Campaign limit reached ({max_allowed})")

    campaign_id = str(uuid.uuid4())
    campaign = {
        "id": campaign_id,
        "company_id": body.company_id,
        "name": body.name,
        "status": "draft",
        "pipeline_mode": body.pipeline_mode,
        "transfer_did": body.transfer_did,
        "transfer_did_backup": body.transfer_did_backup,
        "cadence_days": body.cadence_days,
        "max_attempts": body.max_attempts,
        "call_window_start": body.call_window_start,
        "call_window_end": body.call_window_end,
        "system_prompt": body.system_prompt,
        "greeting_text": body.greeting_text,
        "total_leads": 0,
        "total_calls": 0,
        "total_transfers": 0,
        "qualified_transfers": 0,
        "custom_fields_schema": {},
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _campaigns[campaign_id] = campaign
    _leads[campaign_id] = []

    logger.info("campaign_created", campaign_id=campaign_id, company_id=body.company_id, name=body.name)
    return CampaignResponse(**campaign)


@router.get("/campaigns/{campaign_id}", response_model=CampaignResponse)
async def get_campaign(campaign_id: str):
    """Get campaign details."""
    campaign = _campaigns.get(campaign_id)
    if not campaign:
        raise HTTPException(404, "Campaign not found")
    return CampaignResponse(**campaign)


@router.get("/companies/{company_id}/campaigns", response_model=list)
async def list_campaigns(company_id: str):
    """List all campaigns for a company."""
    return [
        CampaignResponse(**c) for c in _campaigns.values()
        if c["company_id"] == company_id
    ]


# ── Lead Import Endpoint ────────────────────────────────────────────────────


@router.post("/campaigns/{campaign_id}/import")
async def import_leads(
    campaign_id: str,
    file: UploadFile = File(...),
):
    """
    Upload and import a CSV or Excel file of leads into a campaign.

    The system will:
    1. Auto-detect column mappings (phone, name, email, state, etc.)
    2. Validate and normalize phone numbers to E.164
    3. Map state → timezone for call scheduling
    4. Skip duplicates and DNC numbers
    5. Save any unmapped columns as custom fields
    6. Assign each lead to the campaign's call cadence
    """
    campaign = _campaigns.get(campaign_id)
    if not campaign:
        raise HTTPException(404, "Campaign not found")

    # Save uploaded file temporarily
    suffix = os.path.splitext(file.filename or "upload.csv")[1]
    tmp_path = f"/tmp/import_{campaign_id}_{uuid.uuid4().hex[:8]}{suffix}"

    try:
        content = await file.read()
        with open(tmp_path, "wb") as f:
            f.write(content)

        # Get existing phones in this campaign (for dedup)
        existing_phones = {lead["phone"] for lead in _leads.get(campaign_id, [])}

        # Run import
        importer = LeadImporter(
            company_id=campaign["company_id"],
            campaign_id=campaign_id,
            cadence_days=campaign.get("cadence_days", [1, 2, 4, 7, 14, 21]),
            existing_phones=existing_phones,
        )
        result = importer.import_file(tmp_path)

        # Store imported leads
        if hasattr(result, "_parsed_leads"):
            for lead in result._parsed_leads:
                lead_dict = {
                    "id": str(uuid.uuid4()),
                    "company_id": campaign["company_id"],
                    "campaign_id": campaign_id,
                    "phone": lead.phone,
                    "first_name": lead.first_name,
                    "last_name": lead.last_name,
                    "email": lead.email,
                    "state": lead.state,
                    "city": lead.city,
                    "zip_code": lead.zip_code,
                    "timezone": lead.timezone,
                    "status": "new",
                    "attempt_count": 0,
                    "next_call_at": getattr(lead, "_next_call_at", datetime.now(timezone.utc)).isoformat(),
                    "consent_timestamp": lead.consent_timestamp.isoformat() if lead.consent_timestamp else None,
                    "consent_source": lead.consent_source,
                    "custom_fields": lead.custom_fields,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
                _leads[campaign_id].append(lead_dict)

            # Update campaign stats
            campaign["total_leads"] = len(_leads[campaign_id])

            # Update custom fields schema
            if result.custom_fields_detected:
                schema = campaign.get("custom_fields_schema", {})
                for field_name in result.custom_fields_detected:
                    if field_name not in schema:
                        schema[field_name] = {"type": "string", "source": "import"}
                campaign["custom_fields_schema"] = schema

        return {
            "total_rows": result.total_rows,
            "imported": result.imported,
            "skipped_duplicate": result.skipped_duplicate,
            "skipped_invalid_phone": result.skipped_invalid_phone,
            "skipped_dnc": result.skipped_dnc,
            "errors": result.errors[:20],  # Cap at 20 errors
            "column_mapping": result.column_mapping,
            "custom_fields_detected": result.custom_fields_detected,
            "warnings": result.warnings,
        }

    finally:
        # Clean up temp file
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


@router.get("/campaigns/{campaign_id}/leads")
async def list_leads(
    campaign_id: str,
    status: Optional[str] = None,
    limit: int = Query(default=50, le=500),
    offset: int = 0,
):
    """List leads in a campaign with optional status filter."""
    campaign = _campaigns.get(campaign_id)
    if not campaign:
        raise HTTPException(404, "Campaign not found")

    leads = _leads.get(campaign_id, [])

    if status:
        leads = [l for l in leads if l["status"] == status]

    total = len(leads)
    page = leads[offset:offset + limit]

    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "leads": page,
    }


# ── Webhook Configuration ──────────────────────────────────────────────────


@router.put("/companies/{company_id}/webhook")
async def configure_webhook(company_id: str, body: WebhookConfig):
    """Configure the generic webhook URL and secret for a company."""
    company = _companies.get(company_id)
    if not company:
        raise HTTPException(404, "Company not found")

    company["webhook_url"] = body.webhook_url
    company["webhook_secret"] = body.webhook_secret

    logger.info("webhook_configured", company_id=company_id, url=body.webhook_url)
    return {"status": "ok", "webhook_url": body.webhook_url}


# ── Zoho OAuth Endpoints ────────────────────────────────────────────────────


@router.post("/companies/{company_id}/zoho/setup")
async def setup_zoho(company_id: str, body: ZohoSetup):
    """
    Initialize Zoho CRM integration for a company.

    Provide your Zoho OAuth client_id and client_secret.
    Returns an authorization URL for the customer to click.
    """
    company = _companies.get(company_id)
    if not company:
        raise HTTPException(404, "Company not found")

    config = ZohoConfig(
        client_id=body.client_id,
        client_secret=body.client_secret,
        redirect_uri=body.redirect_uri,
    )
    _zoho_configs[company_id] = config

    client = ZohoCRMClient(config)
    auth_url = client.get_authorization_url(state=company_id)

    logger.info("zoho_setup_initiated", company_id=company_id)
    return {
        "status": "pending_authorization",
        "authorization_url": auth_url,
        "instructions": "Share this URL with the customer. They will authorize WellHeard to access their Zoho CRM.",
    }


@router.get("/zoho/callback")
async def zoho_oauth_callback(code: str, state: str = ""):
    """
    Zoho OAuth callback endpoint.

    After the customer authorizes, Zoho redirects here with the code.
    We exchange it for tokens and store them.
    """
    company_id = state
    config = _zoho_configs.get(company_id)
    if not config:
        raise HTTPException(400, "No Zoho setup found for this company")

    client = ZohoCRMClient(config)
    result = await client.exchange_code(code)

    if result["success"]:
        # Store tokens
        _zoho_configs[company_id] = config
        _dispatcher.register_zoho_client(company_id, config)

        # Update company
        if company_id in _companies:
            _companies[company_id]["zoho_configured"] = True

        logger.info("zoho_oauth_complete", company_id=company_id)
        return {
            "status": "connected",
            "message": "Zoho CRM connected successfully. Prospect data will be pushed before transfers.",
        }
    else:
        raise HTTPException(400, f"OAuth failed: {result.get('error', 'unknown')}")


@router.get("/companies/{company_id}/zoho/status")
async def zoho_status(company_id: str):
    """Check Zoho integration status for a company."""
    config = _zoho_configs.get(company_id)
    if not config:
        return {"status": "not_configured"}

    return {
        "status": "connected" if config.is_configured() else "pending",
        "token_valid": config.is_token_valid(),
        "token_expiry": config.token_expiry.isoformat() if config.token_expiry else None,
    }


# ── Helper: Get dispatcher for use by transfer system ───────────────────────


def get_dispatcher() -> PreTransferDispatcher:
    """Return the shared pre-transfer dispatcher."""
    return _dispatcher


def get_company_webhook_config(company_id: str) -> dict:
    """Get webhook URL and secret for a company."""
    company = _companies.get(company_id, {})
    return {
        "webhook_url": company.get("webhook_url", ""),
        "webhook_secret": company.get("webhook_secret", ""),
    }
