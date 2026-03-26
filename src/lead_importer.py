"""
WellHeard AI — Lead Import Engine

Handles CSV/Excel file uploads for bulk lead import:
1. Auto-detects column mappings (phone, first_name, last_name, email, state, etc.)
2. Validates and normalizes phone numbers to E.164
3. Maps US state → IANA timezone (not area code — people move)
4. Assigns each lead to optimal cadence position
5. Saves unmapped columns as custom_fields JSON

Usage:
    importer = LeadImporter(company_id="...", campaign_id="...")
    result = await importer.import_file("/path/to/leads.csv")
    print(result)  # ImportResult with counts and errors
"""

import re
import structlog
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, field
from pathlib import Path

logger = structlog.get_logger()


# ── US State → IANA Timezone Mapping ────────────────────────────────────────
# Based on majority timezone per state. States spanning multiple zones use
# the most populous zone. This is MORE reliable than area code mapping because
# people keep their cell numbers when they move.

STATE_TIMEZONE_MAP = {
    # Eastern Time
    "CT": "America/New_York", "DE": "America/New_York", "DC": "America/New_York",
    "FL": "America/New_York", "GA": "America/New_York", "ME": "America/New_York",
    "MD": "America/New_York", "MA": "America/New_York", "MI": "America/Detroit",
    "NH": "America/New_York", "NJ": "America/New_York", "NY": "America/New_York",
    "NC": "America/New_York", "OH": "America/New_York", "PA": "America/New_York",
    "RI": "America/New_York", "SC": "America/New_York", "VT": "America/New_York",
    "VA": "America/New_York", "WV": "America/New_York", "IN": "America/Indiana/Indianapolis",
    # Central Time
    "AL": "America/Chicago", "AR": "America/Chicago", "IL": "America/Chicago",
    "IA": "America/Chicago", "KS": "America/Chicago", "KY": "America/Kentucky/Louisville",
    "LA": "America/Chicago", "MN": "America/Chicago", "MS": "America/Chicago",
    "MO": "America/Chicago", "NE": "America/Chicago", "ND": "America/Chicago",
    "OK": "America/Chicago", "SD": "America/Chicago", "TN": "America/Chicago",
    "TX": "America/Chicago", "WI": "America/Chicago",
    # Mountain Time
    "AZ": "America/Phoenix", "CO": "America/Denver", "ID": "America/Boise",
    "MT": "America/Denver", "NM": "America/Denver", "UT": "America/Denver",
    "WY": "America/Denver",
    # Pacific Time
    "CA": "America/Los_Angeles", "NV": "America/Los_Angeles",
    "OR": "America/Los_Angeles", "WA": "America/Los_Angeles",
    # Other
    "AK": "America/Anchorage", "HI": "Pacific/Honolulu",
    # Territories
    "PR": "America/Puerto_Rico", "VI": "America/Virgin",
    "GU": "Pacific/Guam", "AS": "Pacific/Pago_Pago", "MP": "Pacific/Guam",
}

# Full state names → abbreviations
STATE_NAME_TO_CODE = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "district of columbia": "DC", "florida": "FL", "georgia": "GA", "hawaii": "HI",
    "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME",
    "maryland": "MD", "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE",
    "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM",
    "new york": "NY", "north carolina": "NC", "north dakota": "ND", "ohio": "OH",
    "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI",
    "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX",
    "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "puerto rico": "PR", "guam": "GU",
}


# ── Column Auto-Detection ───────────────────────────────────────────────────
# Maps common column name variations → our standard field names.

COLUMN_ALIASES = {
    "phone": ["phone", "phone_number", "phonenumber", "telephone", "tel", "mobile",
              "cell", "cell_phone", "cellphone", "phone1", "primary_phone", "contact_phone"],
    "first_name": ["first_name", "firstname", "first", "fname", "given_name", "givenname"],
    "last_name": ["last_name", "lastname", "last", "lname", "surname", "family_name"],
    "email": ["email", "email_address", "emailaddress", "e_mail", "e-mail"],
    "state": ["state", "st", "state_code", "statecode", "province", "region"],
    "city": ["city", "town", "municipality"],
    "zip_code": ["zip", "zip_code", "zipcode", "postal", "postal_code", "postalcode"],
    "consent_timestamp": ["consent_date", "consent_timestamp", "opted_in", "opt_in_date",
                          "consent", "optin_date", "optin"],
    "consent_source": ["consent_source", "source", "lead_source", "leadsource", "form_source"],
}


@dataclass
class ImportResult:
    """Result of a lead import operation."""
    total_rows: int = 0
    imported: int = 0
    skipped_duplicate: int = 0
    skipped_invalid_phone: int = 0
    skipped_dnc: int = 0
    errors: List[Dict[str, Any]] = field(default_factory=list)
    column_mapping: Dict[str, str] = field(default_factory=dict)  # detected_col → our_field
    custom_fields_detected: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


@dataclass
class ParsedLead:
    """A single parsed lead ready for database insertion."""
    phone: str             # E.164 format
    first_name: str = ""
    last_name: str = ""
    email: str = ""
    state: str = ""        # 2-letter code
    city: str = ""
    zip_code: str = ""
    timezone: str = ""     # IANA timezone
    consent_timestamp: Optional[datetime] = None
    consent_source: str = ""
    custom_fields: Dict[str, Any] = field(default_factory=dict)


class LeadImporter:
    """
    Imports leads from CSV or Excel files into a campaign.

    Features:
    - Auto-detects column names from common variations
    - Validates phone numbers (US E.164)
    - Maps state → timezone (not area code)
    - Assigns cadence schedule based on campaign settings
    - Preserves all unmapped columns as custom_fields
    - Deduplicates against existing leads in campaign
    """

    # Default cadence: Day 1, 2, 4, 7, 14, 21
    DEFAULT_CADENCE_DAYS = [1, 2, 4, 7, 14, 21]

    def __init__(
        self,
        company_id: str,
        campaign_id: str,
        cadence_days: Optional[List[int]] = None,
        existing_phones: Optional[set] = None,
        dnc_phones: Optional[set] = None,
    ):
        self.company_id = company_id
        self.campaign_id = campaign_id
        self.cadence_days = cadence_days or self.DEFAULT_CADENCE_DAYS
        self.existing_phones = existing_phones or set()
        self.dnc_phones = dnc_phones or set()

    def import_file(self, file_path: str) -> ImportResult:
        """
        Import leads from a CSV or Excel file.

        Args:
            file_path: Path to CSV or XLSX file.

        Returns:
            ImportResult with counts, mapping info, and any errors.
        """
        import pandas as pd

        result = ImportResult()
        path = Path(file_path)

        # Read file
        try:
            if path.suffix.lower() in [".xlsx", ".xls"]:
                df = pd.read_excel(file_path, dtype=str)
            elif path.suffix.lower() in [".csv", ".tsv"]:
                sep = "\t" if path.suffix.lower() == ".tsv" else ","
                df = pd.read_csv(file_path, dtype=str, sep=sep)
            else:
                result.errors.append({"row": 0, "error": f"Unsupported file type: {path.suffix}"})
                return result
        except Exception as e:
            result.errors.append({"row": 0, "error": f"Failed to read file: {str(e)}"})
            return result

        # Clean column names
        df.columns = [str(c).strip().lower().replace(" ", "_").replace("-", "_") for c in df.columns]
        result.total_rows = len(df)

        # Auto-detect column mapping
        column_mapping = self._detect_columns(list(df.columns))
        result.column_mapping = column_mapping

        # Identify custom fields (columns not mapped to standard fields)
        mapped_cols = set(column_mapping.keys())
        custom_cols = [c for c in df.columns if c not in mapped_cols]
        result.custom_fields_detected = custom_cols

        # Check we have at least a phone column
        phone_col = None
        for col, field_name in column_mapping.items():
            if field_name == "phone":
                phone_col = col
                break

        if not phone_col:
            result.errors.append({"row": 0, "error": "No phone number column detected"})
            return result

        # Parse each row
        parsed_leads: List[ParsedLead] = []

        for idx, row in df.iterrows():
            row_num = idx + 2  # +2 for header row and 0-index

            try:
                lead = self._parse_row(row, column_mapping, custom_cols)
            except Exception as e:
                result.errors.append({"row": row_num, "error": str(e)})
                continue

            # Validate phone
            if not lead.phone:
                result.skipped_invalid_phone += 1
                continue

            # Check DNC
            if lead.phone in self.dnc_phones:
                result.skipped_dnc += 1
                continue

            # Check duplicate
            if lead.phone in self.existing_phones:
                result.skipped_duplicate += 1
                continue

            # Mark as existing to catch within-file duplicates
            self.existing_phones.add(lead.phone)
            parsed_leads.append(lead)
            result.imported += 1

        # Assign cadence schedules
        now = datetime.now(timezone.utc)
        for i, lead in enumerate(parsed_leads):
            # Stagger first calls across the first day to avoid burst
            stagger_minutes = (i % 60) * 2  # Spread across ~2 hours
            lead._next_call_at = now + timedelta(minutes=stagger_minutes)

        logger.info(
            "lead_import_complete",
            company_id=self.company_id,
            campaign_id=self.campaign_id,
            total_rows=result.total_rows,
            imported=result.imported,
            skipped_duplicate=result.skipped_duplicate,
            skipped_invalid_phone=result.skipped_invalid_phone,
            skipped_dnc=result.skipped_dnc,
            custom_fields=result.custom_fields_detected,
        )

        # Store parsed leads on result for caller to use
        result._parsed_leads = parsed_leads
        return result

    def _detect_columns(self, columns: List[str]) -> Dict[str, str]:
        """
        Auto-detect which file columns map to our standard fields.

        Returns: {file_column_name: our_field_name}
        """
        mapping = {}

        for our_field, aliases in COLUMN_ALIASES.items():
            for col in columns:
                clean_col = col.strip().lower().replace(" ", "_").replace("-", "_")
                if clean_col in aliases:
                    mapping[col] = our_field
                    break

        return mapping

    def _parse_row(
        self,
        row: Any,
        column_mapping: Dict[str, str],
        custom_cols: List[str],
    ) -> ParsedLead:
        """Parse a single row into a ParsedLead."""
        lead = ParsedLead(phone="")

        # Extract mapped fields
        for col, field_name in column_mapping.items():
            value = str(row.get(col, "")).strip() if row.get(col) is not None else ""
            if not value or value.lower() == "nan":
                continue

            if field_name == "phone":
                lead.phone = self._normalize_phone(value)
            elif field_name == "first_name":
                lead.first_name = value.title()
            elif field_name == "last_name":
                lead.last_name = value.title()
            elif field_name == "email":
                lead.email = value.lower()
            elif field_name == "state":
                lead.state = self._normalize_state(value)
            elif field_name == "city":
                lead.city = value.title()
            elif field_name == "zip_code":
                lead.zip_code = value.split("-")[0][:5]  # Normalize to 5-digit
            elif field_name == "consent_timestamp":
                lead.consent_timestamp = self._parse_date(value)
            elif field_name == "consent_source":
                lead.consent_source = value

        # Resolve timezone from state
        if lead.state:
            lead.timezone = STATE_TIMEZONE_MAP.get(lead.state, "")

        # Collect custom fields
        for col in custom_cols:
            value = str(row.get(col, "")).strip() if row.get(col) is not None else ""
            if value and value.lower() != "nan":
                lead.custom_fields[col] = value

        return lead

    def _normalize_phone(self, raw: str) -> str:
        """
        Normalize phone number to E.164 format (+1XXXXXXXXXX).

        Handles: (555) 123-4567, 555-123-4567, 5551234567, +15551234567, etc.
        Returns empty string if invalid.
        """
        # Strip everything except digits and leading +
        digits = re.sub(r"[^\d]", "", raw)

        if not digits:
            return ""

        # Handle various formats
        if len(digits) == 10:
            # US 10-digit: add +1
            return f"+1{digits}"
        elif len(digits) == 11 and digits[0] == "1":
            # US 11-digit with country code
            return f"+{digits}"
        elif len(digits) >= 10 and raw.startswith("+"):
            # International format
            return f"+{digits}"
        else:
            return ""  # Invalid

    def _normalize_state(self, raw: str) -> str:
        """Normalize state to 2-letter code."""
        clean = raw.strip()

        # Already a 2-letter code
        if len(clean) == 2:
            return clean.upper()

        # Full state name
        code = STATE_NAME_TO_CODE.get(clean.lower())
        if code:
            return code

        return ""

    def _parse_date(self, raw: str) -> Optional[datetime]:
        """Parse common date formats into datetime."""
        formats = [
            "%Y-%m-%d",
            "%m/%d/%Y",
            "%m-%d-%Y",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M:%S",
            "%m/%d/%Y %H:%M:%S",
            "%m/%d/%Y %I:%M %p",
        ]
        for fmt in formats:
            try:
                return datetime.strptime(raw.strip(), fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
        return None

    def get_next_call_time(self, attempt_count: int, lead_timezone: str) -> datetime:
        """
        Calculate when to make the next call attempt based on cadence.

        Uses Day 1, 2, 4, 7, 14, 21 pattern by default.
        Calls scheduled during optimal windows (10am-12pm, 2-4pm local).
        """
        if attempt_count >= len(self.cadence_days):
            # Beyond cadence — use last interval
            days_delay = self.cadence_days[-1]
        else:
            days_delay = self.cadence_days[attempt_count]

        now = datetime.now(timezone.utc)
        next_date = now + timedelta(days=days_delay)

        # Target 10am local time (start of optimal window)
        # In production, this would use pytz/zoneinfo for exact local time
        # For now, return the date at 10am UTC offset approximation
        next_call = next_date.replace(hour=14, minute=0, second=0, microsecond=0)
        return next_call
