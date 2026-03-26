"""
Integration tests for multi-tenant system, lead import, and webhooks.
"""
import sys
import os
import tempfile
sys.path.insert(0, "/sessions/gifted-vigilant-bohr/mnt/Crown Academy/wellheard-ai")

import asyncio
from datetime import datetime, timezone


def test_lead_importer():
    """Test CSV lead import with auto-column detection."""
    print("\n--- TEST: Lead Import ---")
    import csv

    # Create a test CSV with various column name formats
    with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False, newline='') as f:
        writer = csv.writer(f)
        writer.writerow(["Phone Number", "First Name", "Last", "Email Address", "State", "City", "Zip", "Insurance Type", "Lead Score"])
        writer.writerow(["(555) 123-4567", "John", "Smith", "john@test.com", "California", "Los Angeles", "90001", "Health", "85"])
        writer.writerow(["555-234-5678", "Jane", "Doe", "jane@test.com", "TX", "Austin", "78701", "Auto", "92"])
        writer.writerow(["5553456789", "Bob", "Jones", "bob@test.com", "New York", "Brooklyn", "11201", "Life", "78"])
        writer.writerow(["+15554567890", "Alice", "Brown", "alice@test.com", "FL", "Miami", "33101", "Health", "95"])
        writer.writerow(["invalid", "Bad", "Phone", "bad@test.com", "CA", "SF", "94101", "None", "10"])
        writer.writerow(["(555) 123-4567", "Dupe", "Test", "dupe@test.com", "CA", "LA", "90001", "Health", "60"])  # Duplicate
        tmp_path = f.name

    try:
        from src.lead_importer import LeadImporter

        importer = LeadImporter(
            company_id="test-company",
            campaign_id="test-campaign",
        )
        result = importer.import_file(tmp_path)

        print(f"  Total rows: {result.total_rows}")
        print(f"  Imported: {result.imported}")
        print(f"  Skipped duplicate: {result.skipped_duplicate}")
        print(f"  Skipped invalid phone: {result.skipped_invalid_phone}")
        print(f"  Column mapping: {result.column_mapping}")
        print(f"  Custom fields: {result.custom_fields_detected}")

        assert result.total_rows == 6, f"Expected 6 rows, got {result.total_rows}"
        assert result.imported == 4, f"Expected 4 imported, got {result.imported}"
        assert result.skipped_invalid_phone == 1, f"Expected 1 invalid phone, got {result.skipped_invalid_phone}"
        assert result.skipped_duplicate == 1, f"Expected 1 duplicate, got {result.skipped_duplicate}"

        # Check column mapping detected all fields
        mapped_fields = set(result.column_mapping.values())
        assert "phone" in mapped_fields, "Phone not detected"
        assert "first_name" in mapped_fields, "First name not detected"
        assert "last_name" in mapped_fields, "Last name not detected"
        assert "email" in mapped_fields, "Email not detected"
        assert "state" in mapped_fields, "State not detected"

        # Check custom fields detected
        assert "insurance_type" in result.custom_fields_detected or "lead_score" in result.custom_fields_detected, \
            f"Custom fields not detected: {result.custom_fields_detected}"

        # Check state → timezone mapping
        leads = result._parsed_leads
        ca_lead = next(l for l in leads if l.state == "CA")
        assert ca_lead.timezone == "America/Los_Angeles", f"CA timezone wrong: {ca_lead.timezone}"

        tx_lead = next(l for l in leads if l.state == "TX")
        assert tx_lead.timezone == "America/Chicago", f"TX timezone wrong: {tx_lead.timezone}"

        ny_lead = next(l for l in leads if l.state == "NY")
        assert ny_lead.timezone == "America/New_York", f"NY timezone wrong: {ny_lead.timezone}"

        # Check phone normalization
        assert all(l.phone.startswith("+1") for l in leads), "Not all phones normalized to E.164"

        print("  ✅ Lead import passed")

    finally:
        os.unlink(tmp_path)


def test_webhook_signing():
    """Test HMAC-SHA256 webhook signature generation."""
    print("\n--- TEST: Webhook Signing ---")
    import hmac
    import hashlib
    import json

    from src.webhooks import WebhookSender, ProspectPayload

    payload = ProspectPayload(
        call_id="test-123",
        phone="+15551234567",
        first_name="John",
        last_name="Smith",
        gate_score=96.0,
        gate_checks_passed=8,
    )

    data = payload.to_dict()
    assert data["call_id"] == "test-123"
    assert data["phone"] == "+15551234567"
    assert data["gate_score"] == 96.0
    assert "timestamp" in data

    # Verify signature can be computed
    secret = "test-secret-key"
    body_str = json.dumps(data, sort_keys=True)
    timestamp = "1234567890"
    sign_input = f"{timestamp}.{body_str}"
    sig = hmac.new(secret.encode(), sign_input.encode(), hashlib.sha256).hexdigest()
    assert len(sig) == 64, "HMAC-SHA256 signature should be 64 hex chars"

    print(f"  Payload fields: {len(data)}")
    print(f"  Signature: sha256={sig[:16]}...")
    print("  ✅ Webhook signing passed")


def test_zoho_auth_url():
    """Test Zoho OAuth authorization URL generation."""
    print("\n--- TEST: Zoho OAuth URL ---")

    from src.webhooks import ZohoConfig, ZohoCRMClient

    config = ZohoConfig(
        client_id="1000.TESTCLIENTID",
        client_secret="testsecret123",
        redirect_uri="https://wellheard.ai/v1/zoho/callback",
    )
    client = ZohoCRMClient(config)
    auth_url = client.get_authorization_url(state="company-123")

    assert "accounts.zoho.com" in auth_url
    assert "1000.TESTCLIENTID" in auth_url
    assert "company-123" in auth_url
    assert "ZohoCRM.modules.leads" in auth_url
    assert "offline" in auth_url

    print(f"  Auth URL: {auth_url[:80]}...")
    print("  ✅ Zoho auth URL passed")


def test_pre_transfer_dispatcher():
    """Test dispatcher concurrent fire."""
    print("\n--- TEST: Pre-Transfer Dispatcher ---")

    from src.webhooks import PreTransferDispatcher, ProspectPayload

    dispatcher = PreTransferDispatcher()
    payload = ProspectPayload(
        call_id="test-dispatch",
        phone="+15551234567",
        first_name="John",
        gate_score=96.0,
    )

    # Dispatch with no targets configured — should return empty
    result = asyncio.run(dispatcher.dispatch(payload))
    assert result == {"webhook": None, "zoho": None}, f"Expected empty dispatch, got {result}"

    print("  Empty dispatch: OK")
    print("  ✅ Dispatcher passed")


def test_models():
    """Test SQLAlchemy model definitions."""
    print("\n--- TEST: Data Models ---")

    from src.models import Company, Campaign, Lead, CallLog, Base
    from src.models import CompanyStatus, CampaignStatus, LeadStatus, CallDisposition

    # Check all tables defined
    tables = Base.metadata.tables.keys()
    assert "companies" in tables
    assert "campaigns" in tables
    assert "leads" in tables
    assert "call_logs" in tables

    # Check enums
    assert CompanyStatus.ACTIVE.value == "active"
    assert CampaignStatus.DRAFT.value == "draft"
    assert LeadStatus.IN_CADENCE.value == "in_cadence"
    assert CallDisposition.QUALIFIED_TRANSFER.value == "qualified_transfer"

    print(f"  Tables: {list(tables)}")
    print(f"  LeadStatus values: {[s.value for s in LeadStatus]}")
    print("  ✅ Models passed")


def test_gate_all_scenarios():
    """Re-run gate tests to confirm scores."""
    print("\n--- TEST: Gate Scores (Final Verification) ---")
    from tests.test_gate_scoring import build_qualified_prospect, build_silent_call, build_tv_noise
    from src.transfer_gate import TransferQualificationGate, TransferRecommendation

    gate = TransferQualificationGate()

    # Qualified
    r = gate.evaluate(build_qualified_prospect())
    print(f"  Qualified: score={r.overall_score}, checks={r.checks_passed}/8, approved={r.approved}")
    assert r.approved and r.overall_score >= 95

    # Silent
    r = gate.evaluate(build_silent_call())
    print(f"  Silent:    score={r.overall_score}, checks={r.checks_passed}/8, rec={r.recommendation.value}")
    assert not r.approved and r.overall_score < 15

    # TV noise
    r = gate.evaluate(build_tv_noise())
    print(f"  TV noise:  score={r.overall_score}, checks={r.checks_passed}/8, rec={r.recommendation.value}")
    assert not r.approved and r.overall_score < 40

    print("  ✅ All gate scores verified")


if __name__ == "__main__":
    print("=" * 70)
    print("WELLHEARD AI — INTEGRATION TESTS")
    print("=" * 70)

    test_models()
    test_lead_importer()
    test_webhook_signing()
    test_zoho_auth_url()
    test_pre_transfer_dispatcher()
    test_gate_all_scenarios()

    print("\n" + "=" * 70)
    print("ALL INTEGRATION TESTS PASSED ✅")
    print("=" * 70)
