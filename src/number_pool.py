"""
WellHeard Number Pool Manager

Manages a pool of outbound DIDs (phone numbers) for local presence dialing,
health tracking, warm-up scheduling, and spam avoidance.
"""

import asyncio
import logging
import structlog
from collections import defaultdict, deque
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from enum import Enum
from typing import Dict, List, Optional, Tuple

# Configure structlog
structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
        structlog.processors.JSONRenderer(),
    ],
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    cache_logger_on_first_use=True,
)

log = structlog.get_logger(__name__)


class NumberState(Enum):
    """Lifecycle states for phone numbers in the pool."""
    ACTIVE = "active"
    FLAGGED = "flagged"
    BURNED = "burned"
    RECOVERY = "recovery"
    DECOMMISSIONED = "decommissioned"


# Area code to US state/region mapping (top 50+ area codes)
AREA_CODE_REGION_MAP = {
    "201": "NJ", "202": "DC", "203": "CT", "205": "AL", "206": "WA",
    "207": "ME", "208": "ID", "209": "CA", "210": "TX", "212": "NY",
    "213": "CA", "214": "TX", "215": "PA", "216": "OH", "217": "IL",
    "218": "MN", "219": "IN", "220": "OH", "224": "IL", "225": "LA",
    "227": "MD", "228": "MS", "229": "GA", "231": "MI", "234": "OH",
    "239": "FL", "240": "MD", "248": "MI", "251": "AL", "252": "NC",
    "253": "WA", "254": "TX", "256": "AL", "260": "IN", "262": "WI",
    "267": "PA", "268": "GA", "269": "MI", "270": "KY", "276": "VA",
    "281": "TX", "283": "OH", "289": "ON", "301": "MD", "302": "DE",
    "303": "CO", "304": "WV", "305": "FL", "307": "WY", "308": "NE",
    "309": "IL", "310": "CA", "312": "IL", "313": "MI", "314": "MO",
    "315": "NY", "316": "KS", "317": "IN", "318": "LA", "319": "IA",
    "320": "MN", "321": "FL", "323": "CA", "325": "TX", "330": "OH",
    "331": "IL", "334": "AL", "336": "NC", "337": "LA", "339": "MA",
    "340": "VI", "341": "CA", "347": "NY", "351": "MA", "352": "FL",
    "360": "WA", "361": "TX", "364": "TX", "365": "TN", "367": "DC",
    "369": "TX", "380": "OH", "385": "UT", "386": "FL", "401": "RI",
    "402": "NE", "403": "AB", "404": "GA", "405": "OK", "406": "MT",
    "407": "FL", "408": "CA", "409": "TX", "410": "MD", "412": "PA",
    "413": "MA", "414": "WI", "415": "CA", "416": "ON", "417": "MO",
    "418": "QC", "419": "OH", "420": "PA", "423": "TN", "424": "CA",
    "425": "WA", "428": "PA", "430": "TX", "432": "TX", "434": "VA",
    "435": "UT", "436": "TX", "437": "ON", "438": "QC", "440": "OH",
    "441": "BM", "442": "CA", "443": "MD", "445": "PA", "447": "IL",
    "448": "FL", "450": "QC", "456": "VA", "458": "OR", "459": "TX",
    "460": "VA", "463": "IN", "464": "IL", "469": "TX", "470": "GA",
    "472": "PA", "473": "GD", "475": "CT", "478": "GA", "479": "AR",
    "480": "AZ", "484": "PA", "501": "AR", "502": "KY", "503": "OR",
    "504": "LA", "505": "NM", "506": "NB", "507": "MN", "508": "MA",
    "509": "WA", "510": "CA", "512": "TX", "513": "OH", "514": "QC",
    "515": "IA", "516": "NY", "517": "MI", "518": "NY", "519": "ON",
    "520": "AZ", "521": "PA", "522": "NJ", "523": "MA", "524": "WI",
    "530": "CA", "531": "NE", "534": "WI", "539": "TX", "540": "VA",
    "541": "OR", "551": "NJ", "557": "MO", "559": "CA", "561": "FL",
    "562": "CA", "563": "IA", "564": "WA", "567": "OH", "570": "PA",
    "571": "VA", "572": "VA", "573": "MO", "575": "NM", "578": "MA",
    "580": "OK", "581": "ON", "585": "NY", "586": "MI", "587": "AB",
    "601": "MS", "602": "AZ", "603": "NH", "604": "BC", "605": "SD",
    "606": "KY", "607": "NY", "608": "WI", "609": "NJ", "610": "PA",
    "612": "MN", "613": "ON", "614": "OH", "615": "TN", "616": "MI",
    "617": "MA", "618": "IL", "619": "CA", "620": "KS", "623": "AZ",
    "626": "CA", "628": "CA", "629": "TN", "630": "IL", "631": "NY",
    "636": "MO", "640": "ON", "641": "IA", "645": "NY", "646": "NY",
    "647": "ON", "649": "TC", "650": "CA", "651": "MN", "652": "TN",
    "656": "NC", "657": "CA", "660": "MO", "661": "CA", "662": "MS",
    "664": "MS", "667": "MD", "669": "CA", "670": "MP", "671": "GU",
    "672": "AK", "678": "GA", "681": "WV", "682": "TX", "684": "AS",
    "685": "PA", "686": "AZ", "687": "LA", "701": "ND", "702": "NV",
    "703": "VA", "704": "NC", "705": "ON", "706": "GA", "707": "CA",
    "708": "IL", "709": "NL", "710": "TX", "711": "NA", "712": "IA",
    "713": "TX", "714": "CA", "715": "WI", "716": "NY", "717": "PA",
    "718": "NY", "719": "CO", "720": "CO", "724": "PA", "725": "NV",
    "727": "FL", "728": "PA", "729": "TX", "730": "IL", "731": "TN",
    "732": "NJ", "733": "IL", "734": "MI", "737": "TX", "740": "OH",
    "741": "OH", "742": "VA", "743": "NC", "747": "CA", "748": "ON",
    "754": "FL", "757": "VA", "758": "LC", "760": "CA", "761": "PA",
    "762": "GA", "763": "MN", "764": "CA", "765": "IN", "769": "MS",
    "770": "GA", "771": "DC", "772": "FL", "773": "IL", "774": "MA",
    "775": "NV", "778": "BC", "779": "IL", "780": "AB", "781": "MA",
    "782": "NS", "783": "SC", "786": "FL", "787": "PR", "801": "UT",
    "802": "VT", "803": "SC", "804": "VA", "805": "CA", "806": "TX",
    "807": "ON", "808": "HI", "809": "DO", "810": "MI", "812": "IN",
    "813": "FL", "814": "PA", "815": "IL", "816": "MO", "817": "TX",
    "818": "CA", "819": "QC", "820": "TX", "821": "CA", "823": "SC",
    "825": "AB", "828": "NC", "830": "TX", "831": "CA", "832": "TX",
    "833": "NA", "835": "NA", "838": "NY", "843": "SC", "844": "NA",
    "845": "NY", "847": "IL", "848": "NJ", "850": "FL", "854": "SC",
    "855": "NA", "856": "NJ", "857": "MA", "858": "CA", "859": "KY",
    "860": "CT", "861": "PA", "862": "NJ", "863": "FL", "864": "SC",
    "865": "TN", "866": "NA", "867": "YT", "868": "TT", "869": "KN",
    "870": "AR", "871": "AR", "872": "IL", "873": "QC", "876": "JM",
    "877": "NA", "878": "PA", "880": "NA", "881": "NA", "882": "NA",
    "883": "NA", "884": "NA", "885": "NA", "886": "NA", "887": "NA",
    "888": "NA", "889": "NA", "890": "NA", "898": "NA", "899": "NA",
    "900": "NA", "901": "TN", "902": "NS", "903": "TX", "904": "FL",
    "905": "ON", "906": "MI", "907": "AK", "908": "NJ", "909": "CA",
    "910": "NC", "912": "GA", "913": "KS", "914": "NY", "915": "TX",
    "916": "CA", "917": "NY", "918": "OK", "919": "NC", "920": "WI",
    "921": "PA", "922": "NA", "923": "PA", "924": "PA", "925": "CA",
    "926": "CA", "927": "FL", "928": "AZ", "929": "NY", "930": "IN",
    "931": "TN", "932": "AL", "933": "PA", "934": "IN", "935": "CA",
    "936": "TX", "937": "OH", "938": "AL", "939": "PR", "940": "TX",
    "941": "FL", "942": "PA", "943": "TN", "944": "GA", "945": "TX",
    "946": "PA", "947": "MI", "948": "PA", "949": "CA", "950": "NA",
    "951": "CA", "952": "MN", "953": "TX", "954": "FL", "955": "TX",
    "956": "TX", "957": "NM", "958": "NV", "959": "CT", "960": "MS",
    "961": "LA", "962": "TN", "963": "TX", "964": "CA", "965": "MI",
    "966": "TX", "967": "PA", "968": "OH", "970": "CO", "971": "OR",
    "972": "TX", "973": "NJ", "974": "TN", "975": "MO", "976": "GA",
    "977": "NY", "978": "MA", "979": "TX", "980": "NC", "981": "TN",
    "982": "PA", "983": "TN", "984": "NC", "985": "LA", "986": "PA",
    "987": "MO", "988": "NA", "989": "MI", "990": "NA", "991": "PA",
    "992": "PA", "993": "GA", "994": "MN", "995": "GA", "996": "IN",
    "997": "LA", "998": "PA", "999": "NA",
}


@dataclass
class NumberMetrics:
    """Performance metrics for a single DID."""
    answer_count: int = 0
    total_calls: int = 0
    daily_call_count: int = 0
    last_used_at: Optional[datetime] = None
    flagged_at: Optional[datetime] = None
    burned_at: Optional[datetime] = None
    recovery_started_at: Optional[datetime] = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    call_history: deque = field(default_factory=lambda: deque(maxlen=100))

    def get_answer_rate(self) -> float:
        """Calculate answer rate from last 100 calls."""
        if self.total_calls == 0:
            return 1.0
        return self.answer_count / self.total_calls

    def days_since_creation(self) -> int:
        """How many days since this number was added."""
        return (datetime.utcnow() - self.created_at).days

    def days_in_recovery(self) -> int:
        """How many days since recovery started."""
        if not self.recovery_started_at:
            return 0
        return (datetime.utcnow() - self.recovery_started_at).days

    def to_dict(self) -> dict:
        """Serialize to dictionary."""
        return {
            "answer_count": self.answer_count,
            "total_calls": self.total_calls,
            "daily_call_count": self.daily_call_count,
            "last_used_at": self.last_used_at.isoformat() if self.last_used_at else None,
            "flagged_at": self.flagged_at.isoformat() if self.flagged_at else None,
            "burned_at": self.burned_at.isoformat() if self.burned_at else None,
            "recovery_started_at": self.recovery_started_at.isoformat() if self.recovery_started_at else None,
            "created_at": self.created_at.isoformat(),
            "answer_rate": self.get_answer_rate(),
            "days_since_creation": self.days_since_creation(),
        }

    @staticmethod
    def from_dict(data: dict) -> "NumberMetrics":
        """Deserialize from dictionary."""
        metrics = NumberMetrics()
        metrics.answer_count = data.get("answer_count", 0)
        metrics.total_calls = data.get("total_calls", 0)
        metrics.daily_call_count = data.get("daily_call_count", 0)
        metrics.last_used_at = datetime.fromisoformat(data["last_used_at"]) if data.get("last_used_at") else None
        metrics.flagged_at = datetime.fromisoformat(data["flagged_at"]) if data.get("flagged_at") else None
        metrics.burned_at = datetime.fromisoformat(data["burned_at"]) if data.get("burned_at") else None
        metrics.recovery_started_at = datetime.fromisoformat(data["recovery_started_at"]) if data.get("recovery_started_at") else None
        metrics.created_at = datetime.fromisoformat(data.get("created_at", datetime.utcnow().isoformat()))
        return metrics


@dataclass
class DIDConfig:
    """Configuration for a single DID."""
    number: str
    area_code: str
    provider: str = "telnyx"
    state: NumberState = NumberState.ACTIVE
    metrics: NumberMetrics = field(default_factory=NumberMetrics)

    def to_dict(self) -> dict:
        """Serialize to dictionary."""
        return {
            "number": self.number,
            "area_code": self.area_code,
            "provider": self.provider,
            "state": self.state.value,
            "metrics": self.metrics.to_dict(),
        }

    @staticmethod
    def from_dict(data: dict) -> "DIDConfig":
        """Deserialize from dictionary."""
        return DIDConfig(
            number=data["number"],
            area_code=data["area_code"],
            provider=data.get("provider", "telnyx"),
            state=NumberState(data.get("state", "active")),
            metrics=NumberMetrics.from_dict(data.get("metrics", {})),
        )


class NumberPoolManager:
    """Manages a pool of outbound DIDs for local presence dialing."""

    # Warm-up schedule: day -> max daily calls
    WARMUP_SCHEDULE = {
        1: 10,
        2: 10,
        3: 25,
        4: 25,
        5: 50,
        6: 50,
        7: 50,
        8: 75,
        9: 75,
        10: 75,
    }

    def __init__(
        self,
        numbers: Optional[List[Dict]] = None,
        default_daily_limit: int = 80,
        flagged_daily_limit: int = 30,
        recovery_duration_hours: int = 48,
    ):
        """
        Initialize the number pool manager.

        Args:
            numbers: List of number configs: [{"number": "+1234...", "area_code": "212", "provider": "telnyx"}]
            default_daily_limit: Max calls per number per day (default 80)
            flagged_daily_limit: Max calls for flagged numbers (default 30)
            recovery_duration_hours: Hours to quarantine burned numbers (default 48)
        """
        self.default_daily_limit = default_daily_limit
        self.flagged_daily_limit = flagged_daily_limit
        self.recovery_duration_hours = recovery_duration_hours

        self._lock = asyncio.Lock()
        self._pool: Dict[str, DIDConfig] = {}
        self._last_reset_date = datetime.utcnow().date()

        if numbers:
            for config in numbers:
                self.add_number(
                    config["number"],
                    config["area_code"],
                    config.get("provider", "telnyx"),
                )

        log.info(
            "number_pool_initialized",
            pool_size=len(self._pool),
            default_daily_limit=default_daily_limit,
        )

    def _reset_daily_counts_if_needed(self) -> None:
        """Reset daily call counts if a new day has started."""
        today = datetime.utcnow().date()
        if today > self._last_reset_date:
            for did_config in self._pool.values():
                did_config.metrics.daily_call_count = 0
            self._last_reset_date = today
            log.info("daily_limits_reset")

    def _extract_area_code(self, phone_number: str) -> Optional[str]:
        """Extract area code from E.164 format phone number."""
        # Remove non-digits
        digits = "".join(c for c in phone_number if c.isdigit())

        # Handle +1 US/Canada numbers
        if digits.startswith("1") and len(digits) >= 4:
            return digits[1:4]

        # Handle other lengths or formats
        if len(digits) >= 3:
            return digits[:3]

        return None

    def _get_warmup_limit(self, days_old: int) -> int:
        """Get daily call limit based on warm-up schedule."""
        for day in range(days_old, 0, -1):
            if day in self.WARMUP_SCHEDULE:
                return self.WARMUP_SCHEDULE[day]
        return self.default_daily_limit

    def _get_effective_daily_limit(self, did_config: DIDConfig) -> int:
        """Get the effective daily limit for a number based on its state and age."""
        days_old = did_config.metrics.days_since_creation()

        if did_config.state == NumberState.FLAGGED:
            return self.flagged_daily_limit

        if did_config.state == NumberState.BURNED:
            return 0  # No calls while burned

        if did_config.state == NumberState.RECOVERY:
            # Restart warm-up after recovery
            days_in_recovery = did_config.metrics.days_in_recovery()
            return self._get_warmup_limit(days_in_recovery + 1)

        # Active: use warm-up schedule if < 11 days old, else full limit
        if days_old < 11:
            return self._get_warmup_limit(days_old + 1)

        return self.default_daily_limit

    def _can_use_number(self, did_config: DIDConfig) -> bool:
        """Check if a number can be used for outbound calls."""
        if did_config.state in (NumberState.BURNED, NumberState.DECOMMISSIONED):
            return False

        limit = self._get_effective_daily_limit(did_config)
        return did_config.metrics.daily_call_count < limit

    def _get_region_for_area_code(self, area_code: str) -> Optional[str]:
        """Get state/region for an area code."""
        return AREA_CODE_REGION_MAP.get(area_code)

    def _get_candidates_by_area_code(self, area_code: str) -> List[Tuple[str, DIDConfig]]:
        """Get all usable numbers with matching area code, sorted by LRU."""
        candidates = []
        for number, did_config in self._pool.items():
            if (did_config.area_code == area_code and self._can_use_number(did_config)):
                # Tuple: (last_used_at timestamp, number, config)
                # Sort by oldest last_used_at
                last_used = did_config.metrics.last_used_at or datetime.min
                candidates.append((last_used, number, did_config))

        # Sort by last_used_at (ascending = least recently used first)
        candidates.sort(key=lambda x: x[0])
        return [(num, cfg) for _, num, cfg in candidates]

    def _get_candidates_by_region(self, area_code: str) -> List[Tuple[str, DIDConfig]]:
        """Get all usable numbers in the same region, sorted by LRU."""
        target_region = self._get_region_for_area_code(area_code)
        if not target_region:
            return []

        candidates = []
        for number, did_config in self._pool.items():
            region = self._get_region_for_area_code(did_config.area_code)
            if region == target_region and self._can_use_number(did_config):
                last_used = did_config.metrics.last_used_at or datetime.min
                candidates.append((last_used, number, did_config))

        candidates.sort(key=lambda x: x[0])
        return [(num, cfg) for _, num, cfg in candidates]

    def _get_candidates_any(self) -> List[Tuple[str, DIDConfig]]:
        """Get all usable numbers regardless of area code, sorted by LRU."""
        candidates = []
        for number, did_config in self._pool.items():
            if self._can_use_number(did_config):
                last_used = did_config.metrics.last_used_at or datetime.min
                candidates.append((last_used, number, did_config))

        candidates.sort(key=lambda x: x[0])
        return [(num, cfg) for _, num, cfg in candidates]

    def get_number_for_prospect(self, prospect_number: str) -> Optional[str]:
        """
        Select best outbound number for a prospect using local presence routing.

        Priority:
        1. Exact area code match + remaining capacity (LRU)
        2. Same state/region + remaining capacity (LRU)
        3. Any number with remaining capacity (LRU)

        Args:
            prospect_number: E.164 format phone number of prospect

        Returns:
            DID (phone number) to use, or None if no capacity available
        """
        self._reset_daily_counts_if_needed()

        prospect_area_code = self._extract_area_code(prospect_number)

        if not prospect_area_code:
            log.warning("invalid_prospect_number", number=prospect_number)
            return None

        # Strategy 1: Exact area code match
        candidates = self._get_candidates_by_area_code(prospect_area_code)
        if candidates:
            selected_number, _ = candidates[0]
            log.info(
                "number_selected",
                strategy="exact_area_code",
                selected=selected_number,
                prospect_area_code=prospect_area_code,
            )
            return selected_number

        # Strategy 2: Same region/state
        candidates = self._get_candidates_by_region(prospect_area_code)
        if candidates:
            selected_number, _ = candidates[0]
            log.info(
                "number_selected",
                strategy="same_region",
                selected=selected_number,
                prospect_area_code=prospect_area_code,
            )
            return selected_number

        # Strategy 3: Any available number
        candidates = self._get_candidates_any()
        if candidates:
            selected_number, _ = candidates[0]
            log.info(
                "number_selected",
                strategy="fallback_any",
                selected=selected_number,
                prospect_area_code=prospect_area_code,
            )
            return selected_number

        log.warning("no_available_numbers", prospect_area_code=prospect_area_code)
        return None

    def record_call_outcome(
        self,
        did: str,
        answered: bool,
        duration_s: float = 0,
    ) -> None:
        """
        Record the outcome of a call made from a DID.

        Args:
            did: The DID that made the call
            answered: Whether the call was answered
            duration_s: Call duration in seconds
        """
        if did not in self._pool:
            log.warning("unknown_did", did=did)
            return

        self._reset_daily_counts_if_needed()

        did_config = self._pool[did]
        metrics = did_config.metrics

        # Update metrics
        metrics.total_calls += 1
        metrics.daily_call_count += 1
        metrics.last_used_at = datetime.utcnow()
        metrics.call_history.append(answered)

        if answered:
            metrics.answer_count += 1

        # Check health and update state if needed
        answer_rate = metrics.get_answer_rate()

        if answer_rate >= 0.30:
            # Healthy or recovering
            if did_config.state == NumberState.FLAGGED:
                log.info("number_recovered_from_flagged", did=did, answer_rate=answer_rate)
                did_config.state = NumberState.ACTIVE
            elif did_config.state == NumberState.RECOVERY:
                # Stay in recovery until warm-up complete
                if metrics.days_in_recovery() >= 2:
                    log.info("number_recovered_from_recovery", did=did, answer_rate=answer_rate)
                    did_config.state = NumberState.ACTIVE
        elif answer_rate < 0.20:
            # Burned
            if did_config.state != NumberState.BURNED:
                log.warning("number_burned", did=did, answer_rate=answer_rate)
                did_config.state = NumberState.BURNED
                metrics.burned_at = datetime.utcnow()
                metrics.recovery_started_at = datetime.utcnow()
        elif 0.20 <= answer_rate < 0.30:
            # Flagged
            if did_config.state == NumberState.ACTIVE:
                log.warning("number_flagged", did=did, answer_rate=answer_rate)
                did_config.state = NumberState.FLAGGED
                metrics.flagged_at = datetime.utcnow()

        log.info(
            "call_recorded",
            did=did,
            answered=answered,
            answer_rate=answer_rate,
            total_calls=metrics.total_calls,
            state=did_config.state.value,
        )

    def get_pool_health(self) -> dict:
        """
        Get comprehensive health metrics for all numbers in the pool.

        Returns:
            Dictionary with pool and per-number health information
        """
        self._reset_daily_counts_if_needed()

        pool_stats = {
            "total_numbers": len(self._pool),
            "by_state": defaultdict(int),
            "by_area_code": defaultdict(int),
            "daily_capacity_remaining": 0,
            "numbers": {},
        }

        for number, did_config in self._pool.items():
            limit = self._get_effective_daily_limit(did_config)
            remaining = max(0, limit - did_config.metrics.daily_call_count)
            pool_stats["daily_capacity_remaining"] += remaining

            state = did_config.state.value
            pool_stats["by_state"][state] = pool_stats["by_state"].get(state, 0) + 1
            pool_stats["by_area_code"][did_config.area_code] = pool_stats["by_area_code"].get(did_config.area_code, 0) + 1

            pool_stats["numbers"][number] = {
                "area_code": did_config.area_code,
                "provider": did_config.provider,
                "state": state,
                "answer_rate": did_config.metrics.get_answer_rate(),
                "total_calls": did_config.metrics.total_calls,
                "daily_calls": did_config.metrics.daily_call_count,
                "daily_limit": limit,
                "daily_remaining": remaining,
                "days_old": did_config.metrics.days_since_creation(),
                "last_used_at": did_config.metrics.last_used_at.isoformat() if did_config.metrics.last_used_at else None,
                "flagged_at": did_config.metrics.flagged_at.isoformat() if did_config.metrics.flagged_at else None,
                "burned_at": did_config.metrics.burned_at.isoformat() if did_config.metrics.burned_at else None,
                "recovery_started_at": did_config.metrics.recovery_started_at.isoformat() if did_config.metrics.recovery_started_at else None,
            }

        log.info("pool_health_checked", pool_stats=pool_stats)
        return pool_stats

    def add_number(
        self,
        number: str,
        area_code: str,
        provider: str = "telnyx",
    ) -> None:
        """
        Add a new number to the pool (starts in warm-up mode).

        Args:
            number: E.164 format phone number
            area_code: Area code (e.g., "212")
            provider: Provider name (default: "telnyx")
        """
        if number in self._pool:
            log.warning("number_already_in_pool", number=number)
            return

        self._pool[number] = DIDConfig(
            number=number,
            area_code=area_code,
            provider=provider,
            state=NumberState.ACTIVE,
            metrics=NumberMetrics(),
        )

        log.info(
            "number_added",
            number=number,
            area_code=area_code,
            provider=provider,
        )

    def retire_number(self, number: str) -> None:
        """
        Move a number to Decommissioned state.

        Args:
            number: DID to retire
        """
        if number not in self._pool:
            log.warning("retire_unknown_number", number=number)
            return

        self._pool[number].state = NumberState.DECOMMISSIONED
        log.info("number_retired", number=number)

    def get_available_capacity(self) -> int:
        """
        Get the total number of calls the pool can handle today.

        Returns:
            Total remaining call capacity across all numbers
        """
        self._reset_daily_counts_if_needed()

        capacity = 0
        for did_config in self._pool.values():
            limit = self._get_effective_daily_limit(did_config)
            remaining = max(0, limit - did_config.metrics.daily_call_count)
            capacity += remaining

        return capacity

    def to_dict(self) -> dict:
        """
        Serialize the entire pool to dictionary for persistence.

        Returns:
            Dictionary representation of the pool
        """
        return {
            "config": {
                "default_daily_limit": self.default_daily_limit,
                "flagged_daily_limit": self.flagged_daily_limit,
                "recovery_duration_hours": self.recovery_duration_hours,
            },
            "pool": {number: config.to_dict() for number, config in self._pool.items()},
            "last_reset_date": self._last_reset_date.isoformat(),
        }

    @staticmethod
    def from_dict(data: dict) -> "NumberPoolManager":
        """
        Deserialize a pool from dictionary.

        Args:
            data: Dictionary representation from to_dict()

        Returns:
            Reconstructed NumberPoolManager instance
        """
        manager = NumberPoolManager(
            default_daily_limit=data["config"].get("default_daily_limit", 80),
            flagged_daily_limit=data["config"].get("flagged_daily_limit", 30),
            recovery_duration_hours=data["config"].get("recovery_duration_hours", 48),
        )

        for number, config_dict in data.get("pool", {}).items():
            did_config = DIDConfig.from_dict(config_dict)
            manager._pool[number] = did_config

        if "last_reset_date" in data:
            manager._last_reset_date = datetime.fromisoformat(data["last_reset_date"]).date()

        return manager
