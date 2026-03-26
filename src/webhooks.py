"""
WellHeard AI — Pre-Transfer Webhook & Zoho CRM Integration

Fires prospect data to external systems BEFORE the transfer is initiated,
so the licensed agent already has context when they pick up.

Two modes:
1. Generic Webhook — POST JSON to any URL with HMAC-SHA256 signing
2. Zoho CRM Push — Create/update Lead in customer's Zoho CRM via OAuth 2.0

Architecture:
- Fire-and-forget with 3-second timeout (never blocks the transfer)
- Retry queue for failed deliveries (3 retries, exponential backoff)
- Per-company webhook configuration (URL, secret, Zoho tokens)
- HMAC-SHA256 signature for webhook authentication
- Zoho OAuth 2.0: authorization code → access token → refresh flow
"""

import hmac
import hashlib
import time
import asyncio
import structlog
import httpx
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional, Any, List
from dataclasses import dataclass, field
from urllib.parse import urlencode

logger = structlog.get_logger()


# ── Webhook Payload ──────────────────────────────────────────────────────────


@dataclass
class ProspectPayload:
    """Data sent to external systems before transfer."""
    call_id: str
    phone: str
    first_name: str = ""
    last_name: str = ""
    email: str = ""
    state: str = ""
    city: str = ""

    # Qualification data
    gate_score: float = 0.0
    gate_checks_passed: int = 0
    disposition: str = ""
    call_duration_seconds: float = 0.0

    # Conversation summary
    key_signals: Dict[str, Any] = field(default_factory=dict)
    transcript_summary: str = ""

    # Campaign context
    campaign_id: str = ""
    campaign_name: str = ""
    company_id: str = ""

    # Custom fields from lead record
    custom_fields: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "call_id": self.call_id,
            "phone": self.phone,
            "first_name": self.first_name,
            "last_name": self.last_name,
            "email": self.email,
            "state": self.state,
            "city": self.city,
            "gate_score": self.gate_score,
            "gate_checks_passed": self.gate_checks_passed,
            "disposition": self.disposition,
            "call_duration_seconds": self.call_duration_seconds,
            "key_signals": self.key_signals,
            "transcript_summary": self.transcript_summary,
            "campaign_id": self.campaign_id,
            "campaign_name": self.campaign_name,
            "custom_fields": self.custom_fields,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


# ── Generic Webhook Sender ──────────────────────────────────────────────────


class WebhookSender:
    """
    Sends signed JSON webhooks to external URLs.

    Signing: HMAC-SHA256 with timestamp for replay prevention.
    Headers:
        X-WellHeard-Signature: sha256=<hex_digest>
        X-WellHeard-Timestamp: <unix_timestamp>
    """

    def __init__(self, timeout_seconds: float = 3.0, max_retries: int = 3):
        self.timeout = timeout_seconds
        self.max_retries = max_retries
        self._retry_queue: List[Dict] = []

    async def send(
        self,
        url: str,
        payload: ProspectPayload,
        secret: str,
    ) -> Dict[str, Any]:
        """
        Fire webhook with HMAC-SHA256 signature.

        Returns: {"success": bool, "status_code": int, "error": str}
        """
        if not url:
            return {"success": False, "status_code": 0, "error": "No webhook URL configured"}

        data = payload.to_dict()
        timestamp = str(int(time.time()))

        # Build signature: HMAC-SHA256(secret, timestamp + "." + json_body)
        import json
        body_str = json.dumps(data, sort_keys=True)
        sign_input = f"{timestamp}.{body_str}"
        signature = hmac.new(
            secret.encode("utf-8"),
            sign_input.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        headers = {
            "Content-Type": "application/json",
            "X-WellHeard-Signature": f"sha256={signature}",
            "X-WellHeard-Timestamp": timestamp,
            "X-WellHeard-Event": "pre_transfer",
            "User-Agent": "WellHeard-AI/1.0",
        }

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(url, json=data, headers=headers)
                success = 200 <= response.status_code < 300
                result = {
                    "success": success,
                    "status_code": response.status_code,
                    "error": "" if success else f"HTTP {response.status_code}",
                }
                logger.info(
                    "webhook_sent",
                    url=url,
                    call_id=payload.call_id,
                    status_code=response.status_code,
                    success=success,
                )
                return result
        except httpx.TimeoutException:
            logger.warning("webhook_timeout", url=url, call_id=payload.call_id)
            self._queue_retry(url, payload, secret)
            return {"success": False, "status_code": 0, "error": "Timeout"}
        except Exception as e:
            logger.error("webhook_error", url=url, call_id=payload.call_id, error=str(e))
            self._queue_retry(url, payload, secret)
            return {"success": False, "status_code": 0, "error": str(e)}

    def _queue_retry(self, url: str, payload: ProspectPayload, secret: str):
        """Queue a failed webhook for retry."""
        self._retry_queue.append({
            "url": url,
            "payload": payload,
            "secret": secret,
            "attempts": 1,
            "next_retry_at": time.time() + 10,  # 10s first retry
        })

    async def process_retry_queue(self):
        """Process pending retries (call periodically from background task)."""
        now = time.time()
        remaining = []

        for item in self._retry_queue:
            if now < item["next_retry_at"]:
                remaining.append(item)
                continue

            result = await self.send(item["url"], item["payload"], item["secret"])
            if not result["success"] and item["attempts"] < self.max_retries:
                item["attempts"] += 1
                # Exponential backoff: 10s, 30s, 90s
                item["next_retry_at"] = now + (10 * (3 ** (item["attempts"] - 1)))
                remaining.append(item)
            elif not result["success"]:
                logger.error(
                    "webhook_retry_exhausted",
                    url=item["url"],
                    call_id=item["payload"].call_id,
                    attempts=item["attempts"],
                )

        self._retry_queue = remaining


# ── Zoho CRM Integration ────────────────────────────────────────────────────


@dataclass
class ZohoConfig:
    """Per-company Zoho CRM configuration."""
    client_id: str = ""
    client_secret: str = ""
    redirect_uri: str = ""
    access_token: str = ""
    refresh_token: str = ""
    token_expiry: Optional[datetime] = None
    api_domain: str = "https://www.zohoapis.com"  # .com, .eu, .in, .com.cn, .com.au

    def is_configured(self) -> bool:
        return bool(self.client_id and self.client_secret and self.refresh_token)

    def is_token_valid(self) -> bool:
        if not self.access_token or not self.token_expiry:
            return False
        return datetime.now(timezone.utc) < self.token_expiry - timedelta(minutes=5)


class ZohoCRMClient:
    """
    Zoho CRM OAuth 2.0 client for pushing prospect data.

    OAuth flow:
    1. Generate authorization URL → customer clicks → redirects with code
    2. Exchange code for access_token + refresh_token
    3. Use access_token for API calls (expires in 1 hour)
    4. Auto-refresh using refresh_token when expired

    API: Zoho CRM v7 — Creates Leads with pre-transfer context.
    """

    OAUTH_BASE = "https://accounts.zoho.com/oauth/v2"

    # Map our fields → Zoho CRM Lead fields
    FIELD_MAPPING = {
        "first_name": "First_Name",
        "last_name": "Last_Name",
        "phone": "Phone",
        "email": "Email",
        "city": "City",
        "state": "State",
    }

    def __init__(self, config: ZohoConfig):
        self.config = config

    def get_authorization_url(self, state: str = "") -> str:
        """
        Generate the OAuth authorization URL for a customer to click.

        Args:
            state: Opaque state value (e.g., company_id) for CSRF protection.

        Returns:
            URL string the customer should visit to authorize.
        """
        params = {
            "response_type": "code",
            "client_id": self.config.client_id,
            "redirect_uri": self.config.redirect_uri,
            "scope": "ZohoCRM.modules.leads.CREATE,ZohoCRM.modules.leads.UPDATE,ZohoCRM.modules.notes.CREATE",
            "access_type": "offline",   # Get refresh_token
            "prompt": "consent",        # Always show consent screen
        }
        if state:
            params["state"] = state

        return f"{self.OAUTH_BASE}/auth?{urlencode(params)}"

    async def exchange_code(self, authorization_code: str) -> Dict[str, Any]:
        """
        Exchange authorization code for access + refresh tokens.

        Call this in the OAuth callback endpoint.
        Returns: {"success": bool, "access_token": str, "refresh_token": str, "expires_in": int}
        """
        data = {
            "grant_type": "authorization_code",
            "client_id": self.config.client_id,
            "client_secret": self.config.client_secret,
            "redirect_uri": self.config.redirect_uri,
            "code": authorization_code,
        }

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(f"{self.OAUTH_BASE}/token", data=data)
                result = response.json()

                if "access_token" in result:
                    self.config.access_token = result["access_token"]
                    self.config.refresh_token = result.get("refresh_token", self.config.refresh_token)
                    self.config.token_expiry = datetime.now(timezone.utc) + timedelta(
                        seconds=result.get("expires_in", 3600)
                    )
                    logger.info("zoho_token_exchanged", expires_in=result.get("expires_in"))
                    return {
                        "success": True,
                        "access_token": result["access_token"],
                        "refresh_token": result.get("refresh_token", ""),
                        "expires_in": result.get("expires_in", 3600),
                    }
                else:
                    error = result.get("error", "unknown_error")
                    logger.error("zoho_token_exchange_failed", error=error)
                    return {"success": False, "error": error}
        except Exception as e:
            logger.error("zoho_token_exchange_error", error=str(e))
            return {"success": False, "error": str(e)}

    async def _refresh_token(self) -> bool:
        """Refresh the access token using refresh_token."""
        if not self.config.refresh_token:
            return False

        data = {
            "grant_type": "refresh_token",
            "client_id": self.config.client_id,
            "client_secret": self.config.client_secret,
            "refresh_token": self.config.refresh_token,
        }

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(f"{self.OAUTH_BASE}/token", data=data)
                result = response.json()

                if "access_token" in result:
                    self.config.access_token = result["access_token"]
                    self.config.token_expiry = datetime.now(timezone.utc) + timedelta(
                        seconds=result.get("expires_in", 3600)
                    )
                    logger.info("zoho_token_refreshed")
                    return True
                else:
                    logger.error("zoho_refresh_failed", error=result.get("error", "unknown"))
                    return False
        except Exception as e:
            logger.error("zoho_refresh_error", error=str(e))
            return False

    async def _ensure_token(self) -> bool:
        """Ensure we have a valid access token, refreshing if needed."""
        if self.config.is_token_valid():
            return True
        return await self._refresh_token()

    async def push_prospect(self, payload: ProspectPayload) -> Dict[str, Any]:
        """
        Create or update a Lead in Zoho CRM with prospect data.

        This fires BEFORE the transfer, so the agent has context.
        Also creates a Note with the conversation summary.

        Returns: {"success": bool, "lead_id": str, "error": str}
        """
        if not self.config.is_configured():
            return {"success": False, "lead_id": "", "error": "Zoho not configured"}

        if not await self._ensure_token():
            return {"success": False, "lead_id": "", "error": "Token refresh failed"}

        # Build Zoho Lead record
        lead_data = {}
        for our_field, zoho_field in self.FIELD_MAPPING.items():
            value = getattr(payload, our_field, "")
            if value:
                lead_data[zoho_field] = value

        # Add custom fields from payload
        lead_data["Description"] = (
            f"WellHeard AI Pre-Transfer\n"
            f"Gate Score: {payload.gate_score}/100\n"
            f"Checks Passed: {payload.gate_checks_passed}/8\n"
            f"Call Duration: {payload.call_duration_seconds:.0f}s\n"
            f"Campaign: {payload.campaign_name}\n"
            f"Key Signals: {payload.key_signals}\n"
        )
        lead_data["Lead_Source"] = "WellHeard AI"
        lead_data["Lead_Status"] = "Qualified"

        headers = {
            "Authorization": f"Zoho-oauthtoken {self.config.access_token}",
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                # Search for existing lead by phone
                search_url = (
                    f"{self.config.api_domain}/crm/v7/Leads/search"
                    f"?phone={payload.phone}"
                )
                search_resp = await client.get(search_url, headers=headers)

                if search_resp.status_code == 200:
                    search_data = search_resp.json()
                    existing = search_data.get("data", [])
                    if existing:
                        # Update existing lead
                        lead_id = existing[0]["id"]
                        update_url = f"{self.config.api_domain}/crm/v7/Leads/{lead_id}"
                        resp = await client.put(
                            update_url,
                            json={"data": [lead_data]},
                            headers=headers,
                        )
                    else:
                        # Create new lead
                        resp = await client.post(
                            f"{self.config.api_domain}/crm/v7/Leads",
                            json={"data": [lead_data]},
                            headers=headers,
                        )
                        lead_id = ""
                else:
                    # Search failed — create new
                    resp = await client.post(
                        f"{self.config.api_domain}/crm/v7/Leads",
                        json={"data": [lead_data]},
                        headers=headers,
                    )
                    lead_id = ""

                result_data = resp.json()
                success = resp.status_code in [200, 201]

                if success and not lead_id:
                    # Extract new lead ID
                    details = result_data.get("data", [{}])
                    if details:
                        lead_id = details[0].get("details", {}).get("id", "")

                # Create Note with transcript summary
                if success and lead_id and payload.transcript_summary:
                    note_data = {
                        "data": [{
                            "Note_Title": f"AI Call — {payload.disposition}",
                            "Note_Content": payload.transcript_summary,
                            "Parent_Id": lead_id,
                            "se_module": "Leads",
                        }]
                    }
                    await client.post(
                        f"{self.config.api_domain}/crm/v7/Notes",
                        json=note_data,
                        headers=headers,
                    )

                logger.info(
                    "zoho_push_complete",
                    call_id=payload.call_id,
                    lead_id=lead_id,
                    success=success,
                )
                return {
                    "success": success,
                    "lead_id": lead_id,
                    "error": "" if success else str(result_data),
                }

        except httpx.TimeoutException:
            logger.warning("zoho_push_timeout", call_id=payload.call_id)
            return {"success": False, "lead_id": "", "error": "Timeout"}
        except Exception as e:
            logger.error("zoho_push_error", call_id=payload.call_id, error=str(e))
            return {"success": False, "lead_id": "", "error": str(e)}


# ── Pre-Transfer Dispatcher ─────────────────────────────────────────────────


class PreTransferDispatcher:
    """
    Orchestrates all pre-transfer data pushes.

    Fires both generic webhook AND Zoho CRM push concurrently.
    Never blocks the transfer — uses fire-and-forget with 3s timeout.
    """

    def __init__(self):
        self.webhook_sender = WebhookSender(timeout_seconds=3.0)
        self._zoho_clients: Dict[str, ZohoCRMClient] = {}

    def register_zoho_client(self, company_id: str, config: ZohoConfig):
        """Register a Zoho CRM client for a company."""
        self._zoho_clients[company_id] = ZohoCRMClient(config)

    async def dispatch(
        self,
        payload: ProspectPayload,
        webhook_url: str = "",
        webhook_secret: str = "",
        company_id: str = "",
    ) -> Dict[str, Any]:
        """
        Fire all pre-transfer notifications concurrently.

        Args:
            payload: Prospect data to send
            webhook_url: Generic webhook URL (if configured)
            webhook_secret: HMAC secret for webhook signing
            company_id: Company ID for Zoho CRM lookup

        Returns: {"webhook": result, "zoho": result}
        """
        tasks = []

        # Generic webhook
        if webhook_url:
            tasks.append(("webhook", self.webhook_sender.send(webhook_url, payload, webhook_secret)))

        # Zoho CRM
        zoho_client = self._zoho_clients.get(company_id)
        if zoho_client and zoho_client.config.is_configured():
            tasks.append(("zoho", zoho_client.push_prospect(payload)))

        if not tasks:
            return {"webhook": None, "zoho": None}

        # Fire concurrently with overall 3s timeout
        results = {}
        try:
            async with asyncio.timeout(3.5):
                coros = [coro for _, coro in tasks]
                done = await asyncio.gather(*coros, return_exceptions=True)
                for i, (name, _) in enumerate(tasks):
                    if isinstance(done[i], Exception):
                        results[name] = {"success": False, "error": str(done[i])}
                    else:
                        results[name] = done[i]
        except asyncio.TimeoutError:
            for name, _ in tasks:
                if name not in results:
                    results[name] = {"success": False, "error": "Overall dispatch timeout"}

        logger.info(
            "pre_transfer_dispatched",
            call_id=payload.call_id,
            results={k: v.get("success", False) for k, v in results.items()},
        )

        return results
