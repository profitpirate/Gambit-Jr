from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    return default if value is None else value.strip().lower() in {"1", "true", "yes", "on"}


def _float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def load_dotenv(path: str | Path = ".env") -> None:
    file = Path(path)
    if not file.exists():
        return
    for raw in file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass(slots=True)
class Settings:
    discord_token: str | None = None
    discord_channel_id: int | None = None
    discord_channel_ids: tuple[int, ...] = ()
    discord_webhook_url: str | None = None
    solana_rpc_url: str = "https://api.mainnet-beta.solana.com"
    dexscreener_base_url: str = "https://api.dexscreener.com"
    geckoterminal_base_url: str = "https://api.geckoterminal.com/api/v2"
    bsc_rpc_url: str = "https://bsc-dataseed.bnbchain.org"
    direct_launch_discovery_enabled: bool = False
    pumpfun_discovery_enabled: bool = False
    bnb_launch_discovery_enabled: bool = False
    pumpfun_program_ids: tuple[str, ...] = ("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",)
    bnb_launch_factory_addresses: tuple[str, ...] = ()
    bnb_launch_event_topics: tuple[str, ...] = ()
    bnb_launch_token_topic_index: int = 1
    event_queue_max: int = 2_048
    launch_source_reconnect_seconds: float = 2
    gmgn_enabled: bool = False
    gmgn_api_key: str | None = None
    gmgn_base_url: str = "https://openapi.gmgn.ai"
    gmgn_timeout_seconds: float = 10
    gmgn_cache_ttl_seconds: float = 120
    gmgn_max_retries: int = 2
    gmgn_circuit_failures: int = 4
    gmgn_circuit_cooldown_seconds: float = 60
    gmgn_concurrency: int = 4
    database_path: Path = Path("data/memecoin.db")
    log_level: str = "INFO"
    shadow_mode: bool = True
    shadow_send_alerts: bool = True
    discovery_interval_seconds: float = 30
    max_discoveries_per_cycle: int = 20
    monitor_interval_seconds: float = 30
    candidate_monitor_interval_seconds: float = 30
    candidate_max_age_minutes: float = 180
    candidate_inactivity_timeout_minutes: float = 30
    candidate_max_market_cap_usd: float = 300_000
    min_snapshots_for_momentum: int = 3
    max_active_candidates: int = 250
    snapshot_history_limit: int = 12
    max_active_candidates_per_chain: int = 125
    candidate_retry_initial_seconds: float = 30
    candidate_retry_max_seconds: float = 900
    candidate_retry_backoff: float = 2
    scheduler_fresh_reserved_slots: int = 50
    scheduler_radar_reserved_slots: int = 50
    scheduler_near_signal_reserved_slots: int = 50
    scheduler_genesis_reserved_slots: int = 50
    scheduler_priority_reserved_slots: int = 50
    radar_max_age_minutes: float = 15
    radar_min_liquidity_usd: float = 8_000
    radar_min_snapshots: int = 2
    radar_min_conditions: int = 3
    radar_score_threshold: float = 60
    radar_max_market_cap_usd: float = 150_000
    radar_late_pump_price_change_percent: float = 300
    missed_runner_multiple: float = 5
    major_missed_runner_multiple: float = 10
    outcome_monitor_interval_seconds: float = 300
    outcome_max_age_hours: float = 24
    max_outcome_watchlist: int = 200
    provider_timeout_seconds: float = 10
    provider_max_retries: int = 2
    provider_circuit_failures: int = 4
    provider_circuit_cooldown_seconds: float = 60
    market_max_age_seconds: float = 120
    min_market_cap_usd: float = 10_000
    max_market_cap_usd: float = 300_000
    min_liquidity_usd: float = 10_000
    max_pair_age_minutes: float = 1_440
    max_top10_percent: float = 45
    reject_mint_authority: bool = True
    reject_freeze_authority: bool = True
    watch_threshold: float = 65
    strong_threshold: float = 75
    high_conviction_threshold: float = 85
    min_confidence_for_signal: float = 0.60
    weights: dict[str, float] = field(
        default_factory=lambda: {
            "narrative": 25,
            "social": 20,
            "onchain": 20,
            "developer": 15,
            "momentum": 15,
            "safety": 5,
        }
    )
    milestones: tuple[float, ...] = (1.5, 2, 3, 5, 10, 15, 20, 25, 50, 100)
    failure_multiple: float = 0.30
    inactivity_timeout_hours: float = 24
    alert_cooldown_seconds: float = 900
    health_port: int = 8080
    radar_board_enabled: bool = False
    radar_board_port: int = 8081
    discord_default_alert_tier: str = "HOT_PLUS"
    watchlist_alerts_enabled: bool = False
    daily_report_enabled: bool = False
    min_sample_for_edge_metrics: int = 30
    software_version: str = "1.4.0"
    scoring_version: str = "v1.4-right-tail"
    feature_version: str = "v1.4-t0"
    model_version: str = "deterministic-v1.4"
    radar_version: str = "v1.4-alpha-radar"

    @classmethod
    def from_env(cls) -> Settings:
        load_dotenv()
        channel = os.getenv("DISCORD_CHANNEL_ID")
        channels = tuple(
            int(x.strip()) for x in os.getenv("DISCORD_CHANNEL_IDS", "").split(",") if x.strip()
        )
        if channel and int(channel) not in channels:
            channels = (int(channel), *channels)
        milestones = tuple(
            float(x)
            for x in os.getenv("MILESTONES", "1.5,2,3,5,10,15,20,25,50,100").split(",")
            if x.strip()
        )
        return cls(
            discord_token=os.getenv("DISCORD_TOKEN") or None,
            discord_channel_id=int(channel) if channel else None,
            discord_channel_ids=channels,
            discord_webhook_url=os.getenv("DISCORD_WEBHOOK_URL") or None,
            solana_rpc_url=os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com"),
            dexscreener_base_url=os.getenv("DEXSCREENER_BASE_URL", "https://api.dexscreener.com"),
            geckoterminal_base_url=os.getenv(
                "GECKOTERMINAL_BASE_URL", "https://api.geckoterminal.com/api/v2"
            ),
            bsc_rpc_url=os.getenv("BSC_RPC_URL", "https://bsc-dataseed.bnbchain.org"),
            direct_launch_discovery_enabled=_bool("DIRECT_LAUNCH_DISCOVERY_ENABLED", False),
            pumpfun_discovery_enabled=_bool("PUMPFUN_DISCOVERY_ENABLED", False),
            bnb_launch_discovery_enabled=_bool("BNB_LAUNCH_DISCOVERY_ENABLED", False),
            pumpfun_program_ids=tuple(
                value.strip()
                for value in os.getenv(
                    "PUMPFUN_PROGRAM_IDS",
                    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
                ).split(",")
                if value.strip()
            ),
            bnb_launch_factory_addresses=tuple(
                value.strip()
                for value in os.getenv("BNB_LAUNCH_FACTORY_ADDRESSES", "").split(",")
                if value.strip()
            ),
            bnb_launch_event_topics=tuple(
                value.strip()
                for value in os.getenv("BNB_LAUNCH_EVENT_TOPICS", "").split(",")
                if value.strip()
            ),
            bnb_launch_token_topic_index=_int("BNB_LAUNCH_TOKEN_TOPIC_INDEX", 1),
            event_queue_max=_int("EVENT_QUEUE_MAX", 2_048),
            launch_source_reconnect_seconds=_float("LAUNCH_SOURCE_RECONNECT_SECONDS", 2),
            gmgn_enabled=_bool("GMGN_ENABLED", False),
            gmgn_api_key=os.getenv("GMGN_API_KEY") or None,
            gmgn_base_url=os.getenv("GMGN_BASE_URL", "https://openapi.gmgn.ai").rstrip("/"),
            gmgn_timeout_seconds=_float("GMGN_TIMEOUT_SECONDS", 10),
            gmgn_cache_ttl_seconds=_float("GMGN_CACHE_TTL_SECONDS", 120),
            gmgn_max_retries=_int("GMGN_MAX_RETRIES", 2),
            gmgn_circuit_failures=_int("GMGN_CIRCUIT_FAILURES", 4),
            gmgn_circuit_cooldown_seconds=_float("GMGN_CIRCUIT_COOLDOWN_SECONDS", 60),
            gmgn_concurrency=_int("GMGN_CONCURRENCY", 4),
            database_path=Path(os.getenv("DATABASE_PATH", "data/memecoin.db")),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            shadow_mode=_bool("SHADOW_MODE", True),
            shadow_send_alerts=_bool("SHADOW_SEND_ALERTS", True),
            discovery_interval_seconds=_float("DISCOVERY_INTERVAL_SECONDS", 30),
            max_discoveries_per_cycle=_int("MAX_DISCOVERIES_PER_CYCLE", 20),
            monitor_interval_seconds=_float("MONITOR_INTERVAL_SECONDS", 30),
            candidate_monitor_interval_seconds=_float("CANDIDATE_MONITOR_INTERVAL_SECONDS", 30),
            candidate_max_age_minutes=_float("CANDIDATE_MAX_AGE_MINUTES", 180),
            candidate_inactivity_timeout_minutes=_float("CANDIDATE_INACTIVITY_TIMEOUT_MINUTES", 30),
            candidate_max_market_cap_usd=_float("CANDIDATE_MAX_MARKET_CAP_USD", 300_000),
            min_snapshots_for_momentum=_int("MIN_SNAPSHOTS_FOR_MOMENTUM", 3),
            max_active_candidates=_int("MAX_ACTIVE_CANDIDATES", 250),
            snapshot_history_limit=_int("SNAPSHOT_HISTORY_LIMIT", 12),
            max_active_candidates_per_chain=_int("MAX_ACTIVE_CANDIDATES_PER_CHAIN", 125),
            candidate_retry_initial_seconds=_float("CANDIDATE_RETRY_INITIAL_SECONDS", 30),
            candidate_retry_max_seconds=_float("CANDIDATE_RETRY_MAX_SECONDS", 900),
            candidate_retry_backoff=_float("CANDIDATE_RETRY_BACKOFF", 2),
            scheduler_fresh_reserved_slots=_int(
                "FRESH_RESERVED_SLOTS", _int("SCHEDULER_FRESH_RESERVED_SLOTS", 50)
            ),
            scheduler_radar_reserved_slots=_int("SCHEDULER_RADAR_RESERVED_SLOTS", 50),
            scheduler_near_signal_reserved_slots=_int("SCHEDULER_NEAR_SIGNAL_RESERVED_SLOTS", 50),
            scheduler_genesis_reserved_slots=_int("GENESIS_RESERVED_SLOTS", 50),
            scheduler_priority_reserved_slots=_int("PRIORITY_RESERVED_SLOTS", 50),
            radar_max_age_minutes=_float("RADAR_MAX_AGE_MINUTES", 15),
            radar_min_liquidity_usd=_float("RADAR_MIN_LIQUIDITY_USD", 8_000),
            radar_min_snapshots=_int("RADAR_MIN_SNAPSHOTS", 2),
            radar_min_conditions=_int("RADAR_MIN_CONDITIONS", 3),
            radar_score_threshold=_float("RADAR_SCORE_THRESHOLD", 60),
            radar_max_market_cap_usd=_float("RADAR_MAX_MARKET_CAP_USD", 150_000),
            radar_late_pump_price_change_percent=_float(
                "RADAR_LATE_PUMP_PRICE_CHANGE_PERCENT", 300
            ),
            missed_runner_multiple=_float("MISSED_RUNNER_MULTIPLE", 5),
            major_missed_runner_multiple=_float("MAJOR_MISSED_RUNNER_MULTIPLE", 10),
            outcome_monitor_interval_seconds=_float("OUTCOME_MONITOR_INTERVAL_SECONDS", 300),
            outcome_max_age_hours=_float("OUTCOME_MAX_AGE_HOURS", 24),
            max_outcome_watchlist=_int("MAX_OUTCOME_WATCHLIST", 200),
            provider_timeout_seconds=_float("PROVIDER_TIMEOUT_SECONDS", 10),
            provider_max_retries=_int("PROVIDER_MAX_RETRIES", 2),
            provider_circuit_failures=_int("PROVIDER_CIRCUIT_FAILURES", 4),
            provider_circuit_cooldown_seconds=_float("PROVIDER_CIRCUIT_COOLDOWN_SECONDS", 60),
            market_max_age_seconds=_float("MARKET_MAX_AGE_SECONDS", 120),
            min_market_cap_usd=_float("MIN_MARKET_CAP_USD", 10_000),
            max_market_cap_usd=_float("MAX_MARKET_CAP_USD", 300_000),
            min_liquidity_usd=_float("MIN_LIQUIDITY_USD", 10_000),
            max_pair_age_minutes=_float("MAX_PAIR_AGE_MINUTES", 1_440),
            max_top10_percent=_float("MAX_TOP10_PERCENT", 45),
            reject_mint_authority=_bool("REJECT_MINT_AUTHORITY", True),
            reject_freeze_authority=_bool("REJECT_FREEZE_AUTHORITY", True),
            watch_threshold=_float("WATCH_THRESHOLD", 65),
            strong_threshold=_float("STRONG_THRESHOLD", 75),
            high_conviction_threshold=_float("HIGH_CONVICTION_THRESHOLD", 85),
            min_confidence_for_signal=_float("MIN_CONFIDENCE_FOR_SIGNAL", 0.60),
            weights={
                "narrative": _float("WEIGHT_NARRATIVE", 25),
                "social": _float("WEIGHT_SOCIAL", 20),
                "onchain": _float("WEIGHT_ONCHAIN", 20),
                "developer": _float("WEIGHT_DEVELOPER", 15),
                "momentum": _float("WEIGHT_MOMENTUM", 15),
                "safety": _float("WEIGHT_SAFETY", 5),
            },
            scoring_version=os.getenv("SCORING_VERSION", "v1.4-right-tail"),
            milestones=milestones,
            failure_multiple=_float("FAILURE_MULTIPLE", 0.30),
            inactivity_timeout_hours=_float("INACTIVITY_TIMEOUT_HOURS", 24),
            alert_cooldown_seconds=_float("ALERT_COOLDOWN_SECONDS", 900),
            health_port=_int("HEALTH_PORT", 8080),
            radar_board_enabled=_bool("RADAR_BOARD_ENABLED", False),
            radar_board_port=_int("RADAR_BOARD_PORT", 8081),
            discord_default_alert_tier=os.getenv("DISCORD_DEFAULT_ALERT_TIER", "HOT_PLUS").upper(),
            watchlist_alerts_enabled=_bool("WATCHLIST_ALERTS_ENABLED", False),
            daily_report_enabled=_bool("DAILY_REPORT_ENABLED", False),
            min_sample_for_edge_metrics=_int("MIN_SAMPLE_FOR_EDGE_METRICS", 30),
            software_version=os.getenv("SOFTWARE_VERSION", "1.4.0"),
            feature_version=os.getenv("FEATURE_VERSION", "v1.4-t0"),
            model_version=os.getenv("MODEL_VERSION", "deterministic-v1.4"),
            radar_version=os.getenv("RADAR_VERSION", "v1.4-alpha-radar"),
        )

    def validate(self) -> None:
        if abs(sum(self.weights.values()) - 100) > 0.001:
            raise ValueError("Scoring weights must sum to 100")
        if not (self.watch_threshold < self.strong_threshold < self.high_conviction_threshold):
            raise ValueError("Signal thresholds must be strictly increasing")
        if not 0 <= self.min_confidence_for_signal <= 1:
            raise ValueError("MIN_CONFIDENCE_FOR_SIGNAL must be between 0 and 1")
        if not self.milestones or any(x <= 1 for x in self.milestones):
            raise ValueError("All milestones must be greater than 1")
        if (
            min(
                self.candidate_monitor_interval_seconds,
                self.candidate_max_age_minutes,
                self.candidate_inactivity_timeout_minutes,
                self.max_active_candidates,
                self.min_snapshots_for_momentum,
                self.snapshot_history_limit,
                self.outcome_monitor_interval_seconds,
                self.outcome_max_age_hours,
                self.max_outcome_watchlist,
            )
            <= 0
        ):
            raise ValueError("Candidate monitoring settings must be positive")
        if self.missed_runner_multiple > self.major_missed_runner_multiple:
            raise ValueError("MISSED_RUNNER_MULTIPLE cannot exceed MAJOR_MISSED_RUNNER_MULTIPLE")
        if self.gmgn_enabled and not self.gmgn_api_key:
            raise ValueError("GMGN_ENABLED requires a read-only GMGN_API_KEY")
        if (
            min(
                self.gmgn_timeout_seconds,
                self.gmgn_cache_ttl_seconds,
                self.gmgn_concurrency,
                self.radar_board_port,
            )
            <= 0
        ):
            raise ValueError("GMGN and Radar Board settings must be positive")
        if (
            min(
                self.candidate_retry_initial_seconds,
                self.candidate_retry_max_seconds,
                self.candidate_retry_backoff,
            )
            <= 0
        ):
            raise ValueError("Candidate retry settings must be positive")
        if self.candidate_retry_initial_seconds > self.candidate_retry_max_seconds:
            raise ValueError("Candidate retry initial delay cannot exceed maximum delay")
        if self.discord_default_alert_tier not in {
            "ALL",
            "HOT",
            "PRIORITY",
            "QUALIFIED",
            "GENESIS_ALL",
            "HOT_PLUS",
            "PRIORITY_PLUS",
            "QUALIFIED_ONLY",
        }:
            raise ValueError("Unsupported DISCORD_DEFAULT_ALERT_TIER")
        if min(self.event_queue_max, self.min_sample_for_edge_metrics) <= 0:
            raise ValueError("Event queue and edge sample settings must be positive")

    def config_fingerprint(self) -> str:
        """Stable fingerprint of decision settings; credentials are deliberately excluded."""
        payload = {
            "software_version": self.software_version,
            "scoring_version": self.scoring_version,
            "radar_version": self.radar_version,
            "feature_version": self.feature_version,
            "model_version": self.model_version,
            "weights": self.weights,
            "thresholds": [
                self.watch_threshold,
                self.strong_threshold,
                self.high_conviction_threshold,
                self.min_confidence_for_signal,
            ],
            "radar": [
                self.radar_score_threshold,
                self.radar_min_conditions,
                self.radar_min_liquidity_usd,
                self.radar_max_market_cap_usd,
            ],
            "safety": [
                self.reject_mint_authority,
                self.reject_freeze_authority,
                self.max_top10_percent,
            ],
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()
