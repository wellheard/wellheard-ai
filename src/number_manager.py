"""
WellHeard AI — Number Pool Manager & Rotator
Manages outbound phone numbers with intelligent selection, warming schedules,
cooldown tracking, and answer rate optimization.

Research-backed constraints:
- Local presence (same area code): +27-40% answer rate lift
- Branded Caller ID: +44-133% additional lift
- 50-75 calls/number/day optimal to avoid spam flags
- New numbers: 10-20 calls/day week 1, gradually increasing
- 48-72 hour cooldown between attempts to same prospect
- Number retirement after 30-90 days heavy use
"""
import asyncio
import structlog
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Tuple
from enum import Enum
import random
import phonenumbers

logger = structlog.get_logger()


# ────────────────────────────────────────────────────────────────────────────
# Area Code to State Mapping (Comprehensive US Coverage)
# ────────────────────────────────────────────────────────────────────────────

AREA_CODE_TO_STATE = {
    # New England
    "201": "NJ", "202": "DC", "203": "CT", "205": "AL", "206": "WA",
    "207": "ME", "208": "ID", "209": "CA", "210": "TX", "212": "NY",
    "213": "CA", "214": "TX", "215": "PA", "216": "OH", "217": "IL",
    "218": "MN", "219": "IN", "220": "OH", "223": "PA", "224": "IL",
    "225": "LA", "228": "MS", "229": "GA", "231": "MI", "232": "MD",
    "234": "OH", "236": "MD", "239": "FL", "240": "MD", "248": "MI",
    "249": "ON", "251": "AL", "252": "NC", "253": "WA", "254": "TX",
    "256": "AL", "260": "IN", "262": "WI", "267": "PA", "269": "MI",
    "270": "KY", "272": "PA", "276": "VA", "281": "TX", "282": "OH",
    "283": "OH", "284": "VI", "289": "ON", "301": "MD", "302": "DE",
    "303": "CO", "304": "WV", "305": "FL", "306": "SK", "307": "WY",
    "308": "NE", "309": "IL", "310": "CA", "312": "IL", "313": "MI",
    "314": "MO", "315": "NY", "316": "KS", "317": "IN", "318": "LA",
    "319": "IA", "320": "MN", "321": "FL", "323": "CA", "325": "TX",
    "330": "OH", "331": "IL", "334": "AL", "336": "NC", "337": "LA",
    "339": "MA", "340": "VI", "341": "CA", "343": "ON", "345": "KY",
    "346": "TX", "347": "NY", "351": "MA", "352": "FL", "360": "WA",
    "361": "TX", "364": "TX", "365": "ON", "369": "TX", "372": "IL",
    "373": "OH", "374": "ON", "385": "UT", "386": "FL", "401": "RI",
    "402": "NE", "403": "AB", "404": "GA", "405": "OK", "406": "MT",
    "407": "FL", "408": "CA", "409": "TX", "410": "MD", "412": "PA",
    "413": "MA", "414": "WI", "415": "CA", "416": "ON", "417": "MO",
    "418": "QC", "419": "OH", "420": "OH", "423": "TN", "424": "CA",
    "425": "WA", "428": "ON", "430": "TX", "432": "TX", "434": "VA",
    "435": "UT", "436": "MO", "438": "QC", "440": "OH", "441": "BM",
    "442": "CA", "443": "MD", "445": "PA", "447": "IL", "448": "ON",
    "449": "ON", "450": "QC", "456": "VA", "458": "OR", "460": "WV",
    "461": "OH", "462": "IL", "463": "IN", "464": "WI", "469": "TX",
    "470": "GA", "471": "ON", "472": "ON", "473": "GD", "475": "CT",
    "478": "GA", "479": "AR", "480": "AZ", "484": "PA", "501": "AR",
    "502": "KY", "503": "OR", "504": "LA", "505": "NM", "506": "NB",
    "507": "MN", "508": "MA", "509": "WA", "510": "CA", "512": "TX",
    "513": "OH", "514": "QC", "515": "IA", "516": "NY", "517": "MI",
    "518": "NY", "519": "ON", "520": "AZ", "521": "UT", "530": "CA",
    "531": "NE", "534": "WI", "539": "OK", "540": "VA", "541": "OR",
    "542": "CO", "551": "NJ", "559": "CA", "561": "FL", "562": "CA",
    "563": "IA", "564": "WA", "567": "OH", "570": "PA", "571": "VA",
    "572": "NE", "573": "MO", "574": "IN", "575": "NM", "580": "OK",
    "581": "NS", "585": "NY", "586": "MI", "587": "AB", "601": "MS",
    "602": "AZ", "603": "NH", "605": "SD", "606": "KY", "607": "NY",
    "608": "WI", "609": "NJ", "610": "PA", "612": "MN", "613": "ON",
    "614": "OH", "615": "TN", "616": "MI", "617": "MA", "618": "IL",
    "619": "CA", "620": "KS", "623": "AZ", "624": "CA", "626": "CA",
    "627": "GA", "628": "CA", "629": "TN", "630": "IL", "631": "NY",
    "636": "MO", "639": "SK", "640": "NJ", "641": "IA", "646": "NY",
    "647": "ON", "649": "TC", "650": "CA", "651": "MN", "652": "MS",
    "656": "NJ", "657": "CA", "659": "MS", "660": "MO", "661": "CA",
    "662": "MS", "667": "MD", "669": "CA", "670": "MP", "671": "GU",
    "672": "DC", "678": "GA", "679": "MI", "680": "MI", "681": "WV",
    "682": "TX", "683": "ON", "684": "AS", "685": "LA", "686": "CA",
    "689": "FL", "701": "ND", "702": "NV", "703": "VA", "704": "NC",
    "705": "ON", "706": "GA", "707": "CA", "708": "IL", "709": "NL",
    "710": "US", "712": "IA", "713": "TX", "714": "CA", "715": "WI",
    "716": "NY", "717": "PA", "718": "NY", "719": "CO", "720": "CO",
    "721": "VI", "724": "PA", "725": "NV", "726": "TX", "727": "FL",
    "728": "FL", "729": "TX", "731": "TN", "732": "NJ", "734": "MI",
    "737": "TX", "740": "OH", "742": "ON", "743": "NC", "747": "CA",
    "751": "ON", "754": "FL", "757": "VA", "758": "LC", "760": "CA",
    "761": "TN", "762": "GA", "763": "MN", "765": "IN", "767": "DM",
    "769": "MS", "770": "GA", "771": "DC", "772": "FL", "773": "IL",
    "774": "MA", "775": "NV", "776": "DE", "778": "BC", "779": "IL",
    "780": "AB", "781": "MA", "782": "NS", "783": "WV", "785": "KS",
    "786": "FL", "787": "PR", "801": "UT", "802": "VT", "803": "SC",
    "804": "VA", "805": "CA", "806": "TX", "807": "ON", "808": "HI",
    "809": "DO", "810": "MI", "812": "IN", "813": "FL", "814": "PA",
    "815": "IL", "816": "MO", "817": "TX", "818": "CA", "819": "QC",
    "820": "TX", "822": "TX", "824": "TX", "825": "AB", "828": "NC",
    "829": "DO", "830": "TX", "831": "CA", "832": "TX", "833": "US",
    "835": "US", "838": "NY", "839": "TN", "840": "US", "843": "SC",
    "844": "US", "845": "NY", "846": "TX", "847": "IL", "848": "NJ",
    "849": "DO", "850": "FL", "851": "SC", "852": "HK", "853": "MO",
    "854": "SC", "855": "US", "856": "NJ", "857": "MA", "858": "CA",
    "859": "KY", "860": "CT", "861": "SC", "862": "NJ", "863": "FL",
    "864": "SC", "865": "TN", "866": "US", "867": "YT", "868": "TT",
    "869": "KN", "870": "AR", "871": "AR", "872": "IL", "873": "QC",
    "876": "JM", "877": "US", "878": "PA", "879": "SC", "880": "US",
    "881": "US", "882": "US", "883": "US", "884": "US", "885": "US",
    "886": "US", "887": "US", "888": "US", "889": "US", "890": "TN",
    "898": "FL", "899": "US", "900": "US", "901": "TN", "902": "NS",
    "903": "TX", "904": "FL", "905": "ON", "906": "MI", "907": "AK",
    "908": "NJ", "909": "CA", "910": "NC", "912": "GA", "913": "KS",
    "914": "NY", "915": "TX", "916": "CA", "917": "NY", "918": "OK",
    "919": "NC", "920": "WI", "925": "CA", "927": "FL", "928": "AZ",
    "929": "NY", "930": "NC", "931": "TN", "932": "LA", "934": "NY",
    "936": "TX", "937": "OH", "938": "AL", "939": "PR", "940": "TX",
    "941": "FL", "942": "CA", "943": "GA", "945": "TX", "947": "MI",
    "948": "TX", "949": "CA", "950": "CA", "951": "CA", "952": "MN",
    "953": "TX", "954": "FL", "955": "TX", "956": "TX", "957": "NM",
    "958": "NV", "959": "CT", "960": "MO", "961": "LA", "962": "MO",
    "963": "TX", "964": "CA", "965": "TX", "966": "TX", "967": "MA",
    "968": "ON", "970": "CO", "971": "OR", "972": "TX", "973": "NJ",
    "974": "TN", "975": "MO", "976": "MI", "977": "FL", "978": "MA",
    "979": "TX", "980": "NC", "981": "TN", "982": "TN", "983": "TN",
    "984": "NC", "985": "LA", "986": "ID", "987": "MO", "989": "MI",
}

# ────────────────────────────────────────────────────────────────────────────
# State to Timezone Mapping
# ────────────────────────────────────────────────────────────────────────────

STATE_TO_TIMEZONE = {
    # Eastern Time Zone
    "ME": "America/New_York", "NH": "America/New_York", "VT": "America/New_York",
    "MA": "America/New_York", "RI": "America/New_York", "CT": "America/New_York",
    "NY": "America/New_York", "NJ": "America/New_York", "PA": "America/New_York",
    "DE": "America/New_York", "MD": "America/New_York", "DC": "America/New_York",
    "VA": "America/New_York", "WV": "America/New_York", "OH": "America/New_York",
    "MI": "America/Detroit", "IN": "America/Indiana/Indianapolis",
    "KY": "America/Kentucky/Louisville", "TN": "America/Chicago",
    "MS": "America/Chicago", "AL": "America/Chicago", "GA": "America/New_York",
    "SC": "America/New_York", "NC": "America/New_York", "FL": "America/New_York",
    # Central Time Zone
    "IL": "America/Chicago", "MO": "America/Chicago", "AR": "America/Chicago",
    "LA": "America/Chicago", "IA": "America/Chicago", "MN": "America/Chicago",
    "WI": "America/Chicago", "OK": "America/Chicago", "KS": "America/Chicago",
    "NE": "America/Chicago", "SD": "America/Chicago", "ND": "America/Chicago",
    "TX": "America/Chicago",
    # Mountain Time Zone
    "MT": "America/Denver", "WY": "America/Denver", "CO": "America/Denver",
    "NM": "America/Denver", "UT": "America/Denver", "ID": "America/Boise",
    # Pacific Time Zone
    "WA": "America/Los_Angeles", "OR": "America/Los_Angeles", "CA": "America/Los_Angeles",
    "NV": "America/Los_Angeles",
    # Alaska & Hawaii
    "AK": "America/Anchorage", "HI": "Pacific/Honolulu",
    # US Territories
    "AS": "Pacific/Pago_Pago", "GU": "Pacific/Guam", "MP": "Pacific/Saipan",
    "PR": "America/Puerto_Rico", "VI": "America/Virgin",
    # Canadian Provinces
    "ON": "America/Toronto", "QC": "America/Toronto", "MB": "America/Winnipeg",
    "SK": "America/Regina", "AB": "America/Edmonton", "BC": "America/Vancouver",
    "NL": "America/St_Johns", "NS": "America/Halifax", "NB": "America/Halifax",
    "PE": "America/Halifax", "YT": "America/Anchorage",
}


class NumberStatus(str, Enum):
    """Phone number operational status."""
    ACTIVE = "active"
    WARMING = "warming"
    COOLING = "cooling"
    RETIRED = "retired"
    FLAGGED = "flagged"


@dataclass
class PhoneNumber:
    """
    Represents a single outbound phone number with detailed tracking.
    E.164 format: +1XXXXXXXXXX
    """

    number: str  # E.164 format +1XXXXXXXXXX
    area_code: str  # Extracted area code (e.g. "213")
    state: str  # US state or territory (e.g. "CA")
    status: str = NumberStatus.WARMING.value  # active, warming, cooling, retired, flagged

    purchased_date: datetime = field(default_factory=datetime.utcnow)
    total_calls: int = 0
    calls_today: int = 0
    last_call_time: Optional[datetime] = None
    last_cooldown_start: Optional[datetime] = None

    daily_limit: int = 75  # Max calls/day (adjusted during warming)
    cooldown_minutes: int = 0  # Current cooldown remaining
    warming_day: int = 0  # Days since activation (for warming schedule)
    spam_flags: int = 0  # Number of spam reports
    answer_rate: float = 0.0  # Historical answer rate (0.0-1.0)

    branded_caller_id: bool = False  # Has branded caller ID configured
    stir_shaken_level: str = "A"  # A, B, or C attestation

    def is_available(self) -> bool:
        """Check if number is available for outbound calling right now."""
        if self.status in (NumberStatus.RETIRED.value, NumberStatus.FLAGGED.value):
            return False
        if self.calls_today >= self.daily_limit:
            return False
        if self.cooldown_minutes > 0:
            return False
        return True

    def minutes_until_available(self) -> int:
        """Minutes until this number is available for next call."""
        if self.cooldown_minutes <= 0:
            return 0
        return self.cooldown_minutes

    def can_call_today(self) -> bool:
        """Check if there's capacity for at least one more call today."""
        return self.calls_today < self.daily_limit


@dataclass
class NumberPool:
    """Manages a pool of outbound phone numbers with intelligent selection."""

    numbers: Dict[str, PhoneNumber] = field(default_factory=dict)
    config: Dict = field(default_factory=dict)

    def __post_init__(self):
        """Initialize default config."""
        if not self.config:
            self.config = {
                "cooldown_min_minutes": 15,
                "cooldown_max_minutes": 45,
                "warming_enabled": True,
                "local_presence_enabled": True,
                "retire_after_days": 90,
            }

    def add_number(
        self, number: str, area_code: Optional[str] = None, branded: bool = False
    ) -> PhoneNumber:
        """
        Add a new phone number to the pool.

        Args:
            number: E.164 format number (e.g., "+13105551234")
            area_code: Optional override (auto-detected from number if not provided)
            branded: Whether number has branded caller ID

        Returns:
            PhoneNumber object

        Raises:
            ValueError: If number format is invalid
        """
        # Validate E.164 format
        try:
            parsed = phonenumbers.parse(number, "US")
            if not phonenumbers.is_valid_number(parsed):
                raise ValueError(f"Invalid phone number: {number}")
        except phonenumbers.NumberParseException as e:
            raise ValueError(f"Failed to parse phone number {number}: {e}")

        # Extract area code if not provided
        if not area_code:
            area_code = number[2:5]  # +1XXXXXXXXXXXXX -> XXX

        # Look up state
        state = AREA_CODE_TO_STATE.get(area_code, "US")

        # Create phone number object
        phone = PhoneNumber(
            number=number,
            area_code=area_code,
            state=state,
            status=NumberStatus.WARMING.value,
            purchased_date=datetime.utcnow(),
            branded_caller_id=branded,
        )

        self.numbers[number] = phone
        logger.info(
            "number_added",
            number=number,
            area_code=area_code,
            state=state,
            branded=branded,
        )
        return phone

    def remove_number(self, number: str):
        """Remove/retire a number from the pool."""
        if number in self.numbers:
            self.numbers[number].status = NumberStatus.RETIRED.value
            logger.info("number_retired", number=number)

    def get_best_number(
        self, prospect_area_code: str, prospect_state: str
    ) -> Optional[PhoneNumber]:
        """
        Intelligently select the best available number for a prospect.

        Selection priority:
        1. Same area code (super-local) + available
        2. Same state (semi-local) + available
        3. Any available number
        4. Within each tier: prefer higher answer rate

        Args:
            prospect_area_code: Prospect's area code (e.g., "213")
            prospect_state: Prospect's state (e.g., "CA")

        Returns:
            PhoneNumber or None if no numbers available
        """
        available = [n for n in self.numbers.values() if n.is_available()]

        if not available:
            logger.warning("no_available_numbers", pool_size=len(self.numbers))
            return None

        # Tier 1: Same area code (super-local)
        same_area = [
            n
            for n in available
            if n.area_code == prospect_area_code and self.config.get("local_presence_enabled")
        ]
        if same_area:
            return max(same_area, key=lambda n: n.answer_rate)

        # Tier 2: Same state (semi-local)
        same_state = [n for n in available if n.state == prospect_state]
        if same_state:
            return max(same_state, key=lambda n: n.answer_rate)

        # Tier 3: Any available (prefer higher answer rate)
        return max(available, key=lambda n: n.answer_rate)

    def record_call(self, number: str, answered: bool, duration_seconds: float):
        """
        Record the outcome of a call made from this number.

        Args:
            number: Phone number (E.164)
            answered: Whether call was answered
            duration_seconds: Call duration in seconds
        """
        if number not in self.numbers:
            logger.warning("record_call_unknown_number", number=number)
            return

        phone = self.numbers[number]
        phone.total_calls += 1
        phone.calls_today += 1
        phone.last_call_time = datetime.utcnow()

        # Update answer rate (exponential moving average)
        alpha = 0.1  # weight of new sample
        if phone.total_calls == 1:
            phone.answer_rate = 1.0 if answered else 0.0
        else:
            phone.answer_rate = (
                alpha * (1.0 if answered else 0.0) + (1 - alpha) * phone.answer_rate
            )

        logger.info(
            "call_recorded",
            number=number,
            answered=answered,
            duration=duration_seconds,
            answer_rate=phone.answer_rate,
        )

    def start_cooldown(self, number: str):
        """
        Start a randomized cooldown for a number.
        Randomized between cooldown_min and cooldown_max to avoid patterns.

        Args:
            number: Phone number (E.164)
        """
        if number not in self.numbers:
            return

        phone = self.numbers[number]
        min_cooldown = self.config.get("cooldown_min_minutes", 15)
        max_cooldown = self.config.get("cooldown_max_minutes", 45)

        phone.cooldown_minutes = random.randint(min_cooldown, max_cooldown)
        phone.last_cooldown_start = datetime.utcnow()

        logger.info(
            "cooldown_started",
            number=number,
            cooldown_minutes=phone.cooldown_minutes,
        )

    async def tick_cooldowns(self):
        """
        Decrement cooldown timers (call every minute or use background task).
        In production, use APScheduler or similar.
        """
        for phone in self.numbers.values():
            if phone.cooldown_minutes > 0:
                phone.cooldown_minutes -= 1

    def check_daily_limits(self):
        """
        Reset daily call counters at midnight UTC.
        In production, use APScheduler for daily execution.
        """
        for phone in self.numbers.values():
            phone.calls_today = 0
            # Adjust daily_limit based on warming phase
            if self.config.get("warming_enabled"):
                phone.daily_limit = self._get_warming_schedule(phone)

    def _get_warming_schedule(self, phone: PhoneNumber) -> int:
        """
        Calculate max calls/day based on warming phase.

        Week 1 (0-6 days): 15 calls/day
        Week 2 (7-13 days): 30 calls/day
        Week 3 (14-20 days): 50 calls/day
        Week 4+ (21+ days): 75 calls/day

        Args:
            phone: PhoneNumber object

        Returns:
            Max calls allowed today
        """
        days_old = (datetime.utcnow() - phone.purchased_date).days

        if days_old < 7:
            return 15
        elif days_old < 14:
            return 30
        elif days_old < 21:
            return 50
        else:
            phone.status = NumberStatus.ACTIVE.value
            return 75

    def get_warming_schedule(self, number: str) -> int:
        """
        Public method: get warming schedule for a specific number.

        Returns:
            Max calls/day for this number based on age
        """
        if number not in self.numbers:
            return 75
        return self._get_warming_schedule(self.numbers[number])

    def flag_spam(self, number: str):
        """
        Mark number as potentially spam-flagged by carriers.
        Reduces daily limit to 0 and adjusts status.

        Args:
            number: Phone number (E.164)
        """
        if number not in self.numbers:
            return

        phone = self.numbers[number]
        phone.spam_flags += 1
        phone.daily_limit = 0  # Stop using immediately

        if phone.spam_flags >= 3:
            phone.status = NumberStatus.FLAGGED.value
            logger.warning(
                "number_flagged",
                number=number,
                spam_flags=phone.spam_flags,
            )
        else:
            logger.warning(
                "spam_flag_received",
                number=number,
                spam_flags=phone.spam_flags,
            )

    def get_pool_stats(self) -> Dict:
        """
        Get overall pool health statistics.

        Returns:
            Dictionary with counts and metrics
        """
        statuses = {}
        for phone in self.numbers.values():
            status = phone.status
            statuses[status] = statuses.get(status, 0) + 1

        answer_rates = [p.answer_rate for p in self.numbers.values() if p.answer_rate > 0]
        avg_answer_rate = (
            sum(answer_rates) / len(answer_rates) if answer_rates else 0.0
        )

        total_branded = sum(1 for p in self.numbers.values() if p.branded_caller_id)

        return {
            "total_numbers": len(self.numbers),
            "active": statuses.get(NumberStatus.ACTIVE.value, 0),
            "warming": statuses.get(NumberStatus.WARMING.value, 0),
            "cooling": statuses.get(NumberStatus.COOLING.value, 0),
            "retired": statuses.get(NumberStatus.RETIRED.value, 0),
            "flagged": statuses.get(NumberStatus.FLAGGED.value, 0),
            "avg_answer_rate": round(avg_answer_rate, 3),
            "branded_numbers": total_branded,
            "calls_today": sum(p.calls_today for p in self.numbers.values()),
            "total_capacity": sum(p.daily_limit for p in self.numbers.values()),
        }

    def needs_more_numbers(self, daily_call_target: int) -> int:
        """
        Calculate how many more numbers are needed to meet daily call target.

        Assumes 60 calls/day optimal per number.

        Args:
            daily_call_target: Target number of calls/day for campaign

        Returns:
            Number of additional numbers to purchase (0 if sufficient)
        """
        optimal_per_number = 60
        current_capacity = sum(p.daily_limit for p in self.numbers.values())
        needed_capacity = daily_call_target

        if current_capacity >= needed_capacity:
            return 0

        return max(1, (needed_capacity - current_capacity + optimal_per_number - 1) // optimal_per_number)

    def retire_old_numbers(self, max_age_days: Optional[int] = None):
        """
        Automatically retire heavily-used numbers that exceed max age.

        Args:
            max_age_days: Max age in days (uses config if not provided)
        """
        max_age = max_age_days or self.config.get("retire_after_days", 90)

        for number, phone in self.numbers.items():
            age_days = (datetime.utcnow() - phone.purchased_date).days

            # Retire if old AND heavily used (heavy use = many total calls)
            if age_days > max_age and phone.total_calls > 1000:
                phone.status = NumberStatus.RETIRED.value
                logger.info(
                    "number_auto_retired",
                    number=number,
                    age_days=age_days,
                    total_calls=phone.total_calls,
                )


@dataclass
class NumberRotator:
    """Handles intelligent number rotation for call campaigns."""

    pool: NumberPool = field(default_factory=NumberPool)
    call_history: Dict[Tuple[str, str], List[Dict]] = field(default_factory=dict)

    async def get_outbound_number(
        self, prospect_phone: str, campaign_id: Optional[str] = None
    ) -> Optional[str]:
        """
        Main entry point: Select best outbound number for a prospect.

        Flow:
        1. Extract prospect's area code
        2. Look up prospect's state
        3. Call pool.get_best_number()
        4. Apply cooldown after selection
        5. Return number or None

        Args:
            prospect_phone: Prospect's phone number (E.164 format)
            campaign_id: Optional campaign identifier for logging

        Returns:
            Outbound number (E.164) or None if no numbers available
        """
        try:
            parsed = phonenumbers.parse(prospect_phone, "US")
            # Extract area code from national number (first 3 digits for US)
            prospect_area_code = str(parsed.national_number)[:3]
        except:
            logger.warning("failed_to_parse_prospect_phone", phone=prospect_phone)
            return None

        prospect_state = AREA_CODE_TO_STATE.get(prospect_area_code, "US")

        # Get best number for this prospect
        best_number = self.pool.get_best_number(prospect_area_code, prospect_state)
        if not best_number:
            logger.error(
                "no_available_numbers_for_prospect",
                campaign=campaign_id,
                prospect_state=prospect_state,
            )
            return None

        # Apply cooldown (randomized 15-45 min between calls)
        self.pool.start_cooldown(best_number.number)

        logger.info(
            "outbound_number_selected",
            outbound=best_number.number,
            prospect_state=prospect_state,
            prospect_area_code=prospect_area_code,
            campaign=campaign_id,
            local_match=best_number.area_code == prospect_area_code,
        )

        return best_number.number

    def record_outcome(
        self,
        number: str,
        prospect_phone: str,
        answered: bool,
        duration_seconds: float,
        disposition: str,
    ):
        """
        Record the result of an outbound call.

        Args:
            number: Outbound number used (E.164)
            prospect_phone: Prospect's number (E.164)
            answered: Whether call was answered
            duration_seconds: Call duration
            disposition: Call result (e.g., "answered", "no_answer", "busy", "voicemail")
        """
        # Record in number pool
        self.pool.record_call(number, answered, duration_seconds)

        # Track in call history
        key = (number, prospect_phone)
        if key not in self.call_history:
            self.call_history[key] = []

        self.call_history[key].append(
            {
                "timestamp": datetime.utcnow().isoformat(),
                "answered": answered,
                "duration_seconds": duration_seconds,
                "disposition": disposition,
            }
        )

        logger.info(
            "call_outcome_recorded",
            outbound=number,
            prospect_phone=prospect_phone,
            answered=answered,
            disposition=disposition,
        )

    def get_rotation_stats(self) -> Dict:
        """
        Get detailed rotation statistics.

        Returns:
            Dictionary with answer rates by number and local/semi-local breakdown
        """
        stats = {
            "pool_stats": self.pool.get_pool_stats(),
            "by_number": {},
            "local_match": {"count": 0, "answer_rate": 0.0},
            "semi_local": {"count": 0, "answer_rate": 0.0},
            "non_local": {"count": 0, "answer_rate": 0.0},
        }

        local_rates = []
        semi_local_rates = []
        non_local_rates = []

        for number, phone in self.pool.numbers.items():
            stats["by_number"][number] = {
                "status": phone.status,
                "answer_rate": phone.answer_rate,
                "calls_today": phone.calls_today,
                "total_calls": phone.total_calls,
                "branded": phone.branded_caller_id,
            }

            # Categorize by local match
            if self.call_history:
                for (used_number, prospect_phone), calls in self.call_history.items():
                    if used_number == number:
                        try:
                            prospect_area = prospect_phone[2:5]
                            if prospect_area == phone.area_code:
                                local_rates.extend([1.0 if c["answered"] else 0.0 for c in calls])
                            elif AREA_CODE_TO_STATE.get(prospect_area) == phone.state:
                                semi_local_rates.extend([1.0 if c["answered"] else 0.0 for c in calls])
                            else:
                                non_local_rates.extend([1.0 if c["answered"] else 0.0 for c in calls])
                        except:
                            pass

        # Calculate averages
        if local_rates:
            stats["local_match"]["count"] = len(local_rates)
            stats["local_match"]["answer_rate"] = sum(local_rates) / len(local_rates)

        if semi_local_rates:
            stats["semi_local"]["count"] = len(semi_local_rates)
            stats["semi_local"]["answer_rate"] = sum(semi_local_rates) / len(semi_local_rates)

        if non_local_rates:
            stats["non_local"]["count"] = len(non_local_rates)
            stats["non_local"]["answer_rate"] = sum(non_local_rates) / len(non_local_rates)

        return stats
