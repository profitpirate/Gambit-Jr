from __future__ import annotations

import asyncio
import json
import os
import statistics
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote_plus

import aiohttp

from memecoin_bot.historical.providers import DuneExecutionClient
from memecoin_bot.providers.external import (
    AlchemySolanaClient,
    BirdeyeClient,
    CoinGeckoContextClient,
    ProviderObservation,
    ShyftSolanaClient,
    SolanaTrackerClient,
    SolscanClient,
)
from memecoin_bot.social.public_sources import (
    MastodonPublicClient,
    NeynarFarcasterClient,
    TelegramPublicWebClient,
    YouTubeResearchClient,
)

DOCS_CHECKED_AT = "2026-08-29"
_PROBE_TOKEN = "So11111111111111111111111111111111111111112"


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True, slots=True)
class ProviderCapability:
    provider: str
    role: str
    access_class: str
    credential_env: tuple[str, ...]
    documentation_url: str
    signup_url: str | None
    capabilities: tuple[str, ...]
    rate_limit: dict[str, Any]
    cost: dict[str, Any]
    production_role: str
    credential_required: bool = False
    configuration_sets: tuple[tuple[str, ...], ...] = ()

    def configured(self, environment: dict[str, str] | None = None) -> bool:
        values = environment if environment is not None else os.environ
        if self.configuration_sets:
            return any(all(values.get(name) for name in group) for group in self.configuration_sets)
        return not self.credential_required or all(values.get(name) for name in self.credential_env)


@dataclass(frozen=True, slots=True)
class ProviderProbe:
    provider: str
    started_at: str
    completed_at: str
    state: str
    events_seen: int = 0
    matching_events: int = 0
    tokens_seen: int = 0
    latencies_ms: tuple[float, ...] = ()
    errors: tuple[str, ...] = ()
    coverage: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)

    def persisted(self) -> dict[str, Any]:
        values = asdict(self)
        values["probe_id"] = str(uuid.uuid4())
        values["latency_p50_ms"] = (
            statistics.median(self.latencies_ms) if self.latencies_ms else None
        )
        values["latency_p95_ms"] = _percentile(self.latencies_ms, 0.95)
        values["error_count"] = len(self.errors)
        return values


def capabilities() -> tuple[ProviderCapability, ...]:
    """Dated provider facts from official documentation, not scoring policy."""
    return (
        ProviderCapability(
            "native_solana_rpc",
            "canonical chain truth and Pump.fun program/account monitoring",
            "KEYLESS_PUBLIC_FALLBACK",
            (),
            "https://solana.com/docs/rpc/websocket",
            None,
            ("logsSubscribe", "accountSubscribe", "getTransaction", "getSignaturesForAddress"),
            {"provider_dependent": True, "public_endpoint_not_for_primary_production": True},
            {"monthly_usd": 0},
            "PRIMARY_TRUTH_WITH_OPERATOR_RPC_RECOMMENDED",
        ),
        ProviderCapability(
            "helius",
            "selective low-latency Solana RPC and standard WebSocket redundancy",
            "FREE_CREDIT_METERED",
            ("HELIUS_API_KEY",),
            "https://www.helius.dev/docs/billing/credits",
            "https://dashboard.helius.dev/signup",
            ("standard_rpc", "standard_websocket", "selective_curated_accounts"),
            {"free_rps": 10, "free_websocket_connections": 5},
            {"monthly_usd": 0, "monthly_credits": 1_000_000, "wss_metered": True},
            "PRIMARY_SELECTIVE_WHEN_ADMITTED",
            True,
        ),
        ProviderCapability(
            "alchemy",
            "secondary standard Solana RPC and WebSocket transport",
            "FREE_CREDIT_METERED",
            ("ALCHEMY_API_KEY", "ALCHEMY_SOLANA_RPC_URL", "ALCHEMY_SOLANA_WSS_URL"),
            "https://www.alchemy.com/docs/reference/solana-subscription-api-endpoints",
            "https://www.alchemy.com/",
            ("getSlot", "slotSubscribe", "standard_solana_rpc"),
            {"bandwidth_metered": True, "plan_dependent": True},
            {"monthly_usd": 0, "free_credit_metered": True},
            "SECONDARY_SOLANA_TRANSPORT",
            True,
            (("ALCHEMY_API_KEY",), ("ALCHEMY_SOLANA_RPC_URL",)),
        ),
        ProviderCapability(
            "shyft",
            "tertiary standard Solana RPC fallback",
            "FREE_CREDIT_METERED",
            ("SHYFT_API_KEY", "SHYFT_SOLANA_RPC_URL"),
            "https://docs.shyft.to/solana/shyft-rpcs",
            "https://shyft.to/",
            ("getSlot", "standard_solana_rpc"),
            {"plan_dependent": True},
            {"monthly_usd": 0, "credit_metered": True},
            "TERTIARY_SOLANA_FALLBACK",
            True,
            (("SHYFT_API_KEY",), ("SHYFT_SOLANA_RPC_URL",)),
        ),
        ProviderCapability(
            "solana_tracker",
            "independent Solana RPC, WebSocket and indexed token intelligence",
            "FREE_CREDIT_METERED",
            (
                "SOLANA_TRACKER_API_KEY",
                "SOLANA_TRACKER_RPC_URL",
                "SOLANA_TRACKER_WSS_URL",
            ),
            "https://docs.solanatracker.io/quickstart",
            "https://www.solanatracker.io/data-api",
            ("getSlot", "slotSubscribe", "token_price", "token_metadata", "wallet_lookup"),
            {"plan_dependent": True, "single_websocket_probe": True},
            {"monthly_usd": 0, "credit_metered": True},
            "INDEPENDENT_CORROBORATION",
            True,
            (("SOLANA_TRACKER_API_KEY",),),
        ),
        ProviderCapability(
            "birdeye",
            "wallet, holder, sniper, bundle, insider and smart-money enrichment",
            "PLAN_DEPENDENT_CREDIT_METERED",
            ("BIRDEYE_API_KEY",),
            "https://docs.birdeye.so/reference/get-token-v1-holder-profile",
            "https://bds.birdeye.so/",
            (
                "holder_profile",
                "holder_count_when_available",
                "wallet_activity_when_available",
                "plan_exposed_labels_only",
            ),
            {"holder_profile_compute_units": 35, "plan_dependent": True},
            {"credit_metered": True, "paid_fields_not_assumed": True},
            "OPTIONAL_ACTOR_ENRICHMENT",
            True,
        ),
        ProviderCapability(
            "solscan",
            "indexed Solana transaction, token and holder cross-check",
            "PLAN_DEPENDENT_CREDIT_METERED",
            ("SOLSCAN_API_KEY",),
            "https://pro-api.solscan.io/pro-api-docs/v2.0/reference/v2-token-holders",
            "https://solscan.io/apis",
            ("token_holders", "token_metadata", "transaction_lookup"),
            {"request_compute_units": 100, "probe_page_size": 10},
            {"credit_metered": True},
            "OPTIONAL_INDEXED_CROSS_CHECK",
            True,
        ),
        ProviderCapability(
            "coingecko",
            "slow SOL and broad crypto regime context",
            "KEYLESS_OR_DEMO_KEY",
            ("COINGECKO_API_KEY",),
            "https://docs.coingecko.com/reference/simple-price",
            "https://www.coingecko.com/en/api",
            ("sol_price", "sol_24h_change", "broad_market_context"),
            {"public_rate_limit_dynamic": True, "retry_after_honoured": True},
            {"monthly_usd": 0, "demo_key_optional": True},
            "SLOW_REGIME_CONTEXT_ONLY",
        ),
        ProviderCapability(
            "pumpportal",
            "redundant Pump.fun token creation and migration events",
            "FREE_STREAM_CREDENTIAL_REQUIRED",
            ("PUMPPORTAL_API_KEY",),
            "https://pumpportal.fun/data-api/real-time/",
            "https://pumpportal.fun/",
            ("subscribeNewToken", "subscribeMigration"),
            {"websocket_connections": 1},
            {"creation_and_migration": "FREE", "trade_streams_used": False},
            "SECONDARY_REDUNDANCY",
            True,
        ),
        ProviderCapability(
            "dexscreener",
            "batched market state and promotional/social metadata events",
            "KEYLESS_PUBLIC",
            (),
            "https://docs.dexscreener.com/api/reference",
            None,
            (
                "token_batch_30",
                "profiles",
                "community_takeovers",
                "ads",
                "boosts",
            ),
            {"market_rpm": 300, "profile_event_rpm": 60, "token_batch_max": 30},
            {"monthly_usd": 0},
            "MARKET_AND_PROMOTIONAL_ENRICHMENT",
        ),
        ProviderCapability(
            "geckoterminal",
            "new-pool and post-migration market fallback",
            "KEYLESS_PUBLIC",
            (),
            "https://apiguide.geckoterminal.com/faq",
            None,
            ("new_pools", "pool_market", "ohlcv", "latest_trades"),
            {"public_rpm": 30},
            {"monthly_usd": 0},
            "SECONDARY_MARKET_FALLBACK",
        ),
        ProviderCapability(
            "dune",
            "targeted month-partitioned historical Pump.fun research",
            "FREE_CREDIT_METERED",
            ("DUNE_API_KEY",),
            "https://docs.dune.com/api-reference/executions/endpoint/execute-sql",
            "https://dune.com/auth/register",
            (
                "repository_owned_direct_sql",
                "saved_query_optional_fallback",
                "execution_polling",
                "paginated_results",
            ),
            {"free_execute_rpm": 15, "free_result_rpm": 40},
            {"monthly_usd": 0, "execution_is_credit_metered": True},
            "HISTORICAL_BACKBONE",
            True,
        ),
        ProviderCapability(
            "bluesky_jetstream",
            "keyless narrative and contract-address observation",
            "KEYLESS_PUBLIC",
            (),
            "https://bsky.network/docs/jetstream/",
            None,
            ("public_live_tail", "server_side_collection_filter", "cursor_resume"),
            {"collection_filters": 100, "did_filters": 10_000},
            {"monthly_usd": 0, "authentication_required": False},
            "SOCIAL_OBSERVATION",
        ),
        ProviderCapability(
            "neynar_farcaster",
            "crypto-native Farcaster mention and author-spread research",
            "CREDIT_METERED_READ_ONLY",
            ("NEYNAR_API_KEY",),
            "https://docs.neynar.com/reference/search-casts",
            "https://neynar.com/",
            ("cast_search", "unique_authors", "engagement", "first_mention"),
            {"plan_dependent": True, "probe_limit": 10},
            {"credit_metered": True},
            "RESEARCH_ONLY_SOCIAL_ENRICHMENT",
            True,
        ),
        ProviderCapability(
            "youtube",
            "high-priority candidate and sampled narrative research",
            "FREE_QUOTA_METERED",
            ("YOUTUBE_API_KEY",),
            "https://developers.google.com/youtube/v3/docs/search/list",
            "https://console.cloud.google.com/apis/library/youtube.googleapis.com",
            ("video_search", "channel_spread", "views", "comments", "publication_recency"),
            {"quota_metered": True, "aggressive_cache_required": True, "probe_max_results": 5},
            {"monthly_usd": 0, "daily_quota_limited": True},
            "RESEARCH_ONLY_HIGH_PRIORITY_SOCIAL",
            True,
        ),
        ProviderCapability(
            "telegram_public_web",
            "public Telegram channel preview narrative research without MTProto",
            "KEYLESS_CONFIGURED_PUBLIC",
            ("TELEGRAM_PUBLIC_CHANNELS", "TELEGRAM_CHANNELS"),
            "https://telegram.org/faq_channels",
            None,
            ("public_preview", "recent_messages", "first_mention", "visible_forwards"),
            {"minimum_request_interval_seconds": 1, "private_channels_unsupported": True},
            {"monthly_usd": 0},
            "OPTIONAL_PUBLIC_SOCIAL_RESEARCH",
            False,
        ),
        ProviderCapability(
            "mastodon",
            "multi-instance public hashtag, name and contract-address research",
            "KEYLESS_OR_OPTIONAL_TOKEN",
            ("MASTODON_INSTANCE_URLS", "MASTODON_INSTANCE_URL", "MASTODON_ACCESS_TOKEN"),
            "https://docs.joinmastodon.org/methods/search/",
            None,
            ("multi_instance_fallback", "public_search", "author_spread", "engagement"),
            {"instance_specific": True, "fallback_sequential": True},
            {"monthly_usd": 0, "access_token_optional": True},
            "OPTIONAL_PUBLIC_SOCIAL_RESEARCH",
            False,
        ),
        ProviderCapability(
            "discord_authorized",
            "mentions in guild channels explicitly visible to the Gambit bot",
            "OPERATOR_AUTHORIZED",
            ("DISCORD_TOKEN", "DISCORD_SOCIAL_CHANNEL_IDS"),
            "https://discord.com/developers/docs/events/gateway#message-content-intent",
            "https://discord.com/developers/applications",
            ("authorized_channel_mentions", "unique_authors", "channel_diversity"),
            {"gateway_rate_limits_apply": True},
            {"monthly_usd": 0},
            "OPTIONAL_SOCIAL_OBSERVATION",
            True,
        ),
        ProviderCapability(
            "telegram",
            "authorized public-channel narrative observation through MTProto",
            "OPERATOR_AUTHORIZED",
            ("TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TELEGRAM_SESSION"),
            "https://core.telegram.org/api/obtaining_api_id",
            "https://my.telegram.org/",
            ("authorized_channel_mentions", "unique_authors", "forwards"),
            {"server_flood_waits_must_be_honoured": True},
            {"monthly_usd": 0},
            "OPTIONAL_SOCIAL_OBSERVATION",
            True,
        ),
        ProviderCapability(
            "reddit",
            "slow narrative emergence and cross-platform confirmation",
            "REGISTERED_COMPLIANT_ONLY",
            ("REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET"),
            "https://redditinc.com/policies/data-api-terms",
            "https://developers.reddit.com/",
            ("targeted_community_read",),
            {"dynamic_and_app_review_dependent": True},
            {"commercial_or_excess_use_requires_separate_agreement": True},
            "RESEARCH_ONLY_UNTIL_APPROVED",
            True,
        ),
        ProviderCapability(
            "x_direct_api",
            "direct X social observation",
            "UNAVAILABLE_FREE",
            (),
            "https://docs.x.com/x-api/getting-started/pricing",
            None,
            ("paid_reads_only",),
            {"monthly_post_read_cap": 3_000_000},
            {"post_read_usd_each": 0.005, "free_read_tier": False},
            "REJECTED_NO_USEFUL_FREE_READ_PATH",
        ),
    )


class ProviderRegistry:
    def __init__(self, store: Any, environment: dict[str, str] | None = None):
        self.store = store
        self.environment = environment if environment is not None else dict(os.environ)

    def refresh(self) -> list[dict[str, Any]]:
        output = []
        now = _now()
        with self.store._lock, self.store.conn:
            for item in capabilities():
                configured = item.configured(self.environment)
                if item.access_class == "UNAVAILABLE_FREE":
                    admission = "REJECTED"
                elif configured and not item.credential_required:
                    admission = "PENDING_LIVE_PROBE"
                elif configured:
                    admission = "CONFIGURED_PENDING_LIVE_PROBE"
                else:
                    admission = "BLOCKED_EXTERNAL_CREDENTIAL"
                existing = self.store.conn.execute(
                    "SELECT configured,admission_state FROM provider_capabilities_v15 "
                    "WHERE provider=?",
                    (item.provider,),
                ).fetchone()
                if (
                    existing
                    and bool(existing["configured"]) == configured
                    and existing["admission_state"]
                    not in {
                        "PENDING_LIVE_PROBE",
                        "CONFIGURED_PENDING_LIVE_PROBE",
                        "BLOCKED_EXTERNAL_CREDENTIAL",
                    }
                ):
                    admission = str(existing["admission_state"])
                self.store.conn.execute(
                    "INSERT INTO provider_capabilities_v15(provider,role,access_class,"
                    "credential_required,credential_env_json,documentation_url,signup_url,"
                    "current_docs_checked_at,capabilities_json,rate_limit_json,cost_json,"
                    "production_role,configured,admission_state,updated_at) VALUES(?,?,?,?,?,?,?,?,"
                    "?,?,?,?,?,?,?) ON CONFLICT(provider) DO UPDATE SET role=excluded.role,"
                    "access_class=excluded.access_class,credential_required=excluded.credential_required,"
                    "credential_env_json=excluded.credential_env_json,documentation_url="
                    "excluded.documentation_url,signup_url=excluded.signup_url,current_docs_checked_at="
                    "excluded.current_docs_checked_at,capabilities_json=excluded.capabilities_json,"
                    "rate_limit_json=excluded.rate_limit_json,cost_json=excluded.cost_json,"
                    "production_role=excluded.production_role,configured=excluded.configured,"
                    "admission_state=excluded.admission_state,updated_at=excluded.updated_at",
                    (
                        item.provider,
                        item.role,
                        item.access_class,
                        int(item.credential_required),
                        _json(item.credential_env),
                        item.documentation_url,
                        item.signup_url,
                        DOCS_CHECKED_AT,
                        _json(item.capabilities),
                        _json(item.rate_limit),
                        _json(item.cost),
                        item.production_role,
                        int(configured),
                        admission,
                        now,
                    ),
                )
                output.append(
                    {
                        "provider": item.provider,
                        "configured": configured,
                        "admission_state": admission,
                        "required_env": list(item.credential_env),
                    }
                )
        return output

    async def probe(
        self, providers: set[str] | None = None, timeout: float = 12
    ) -> list[dict[str, Any]]:
        self.refresh()
        selected = providers or {
            "native_solana_rpc",
            "dexscreener",
            "geckoterminal",
            "bluesky_jetstream",
            "helius",
            "pumpportal",
            "dune",
            "birdeye",
            "solana_tracker",
            "alchemy",
            "shyft",
            "solscan",
            "coingecko",
            "neynar_farcaster",
            "youtube",
            "telegram_public_web",
            "mastodon",
        }
        probes: list[ProviderProbe] = []
        for item in capabilities():
            if item.provider not in selected:
                continue
            if item.access_class == "UNAVAILABLE_FREE":
                continue
            if not item.configured(self.environment):
                probes.append(
                    ProviderProbe(
                        item.provider,
                        _now(),
                        _now(),
                        "BLOCKED_EXTERNAL",
                        errors=("required credentials are not configured",),
                        evidence={"required_env": list(item.credential_env)},
                    )
                )
                continue
            probes.append(await self._probe_one(item.provider, timeout))
        persisted = [self._persist_probe(probe) for probe in probes]
        return persisted

    async def _probe_one(self, provider: str, timeout: float) -> ProviderProbe:
        started = _now()
        try:
            if provider == "native_solana_rpc":
                return await self._probe_solana(started, timeout)
            if provider == "helius":
                return await self._probe_helius(started, timeout)
            if provider == "dexscreener":
                return await self._probe_dexscreener(started, timeout)
            if provider == "geckoterminal":
                return await self._probe_gecko(started, timeout)
            if provider == "bluesky_jetstream":
                return await self._probe_bluesky(started, timeout)
            if provider == "pumpportal":
                return await self._probe_pumpportal(started, timeout)
            if provider == "dune":
                return await self._probe_dune(started, timeout)
            if provider == "birdeye":
                observation = await BirdeyeClient(
                    self.environment["BIRDEYE_API_KEY"],
                    base_url=self.environment.get(
                        "BIRDEYE_BASE_URL", "https://public-api.birdeye.so"
                    ),
                ).probe(_PROBE_TOKEN, timeout)
                return _observation_probe(provider, started, "ENRICHMENT", observation)
            if provider == "solana_tracker":
                observation = await SolanaTrackerClient(
                    self.environment["SOLANA_TRACKER_API_KEY"],
                    rpc_url=self.environment.get("SOLANA_TRACKER_RPC_URL"),
                    wss_url=self.environment.get("SOLANA_TRACKER_WSS_URL"),
                    data_url=self.environment.get(
                        "SOLANA_TRACKER_DATA_URL", "https://data.solanatracker.io"
                    ),
                ).probe(_PROBE_TOKEN, timeout)
                return _observation_probe(provider, started, "INDEPENDENT_CORROBORATION", observation)
            if provider == "alchemy":
                observation = await AlchemySolanaClient(
                    api_key=self.environment.get("ALCHEMY_API_KEY"),
                    rpc_url=self.environment.get("ALCHEMY_SOLANA_RPC_URL"),
                    wss_url=self.environment.get("ALCHEMY_SOLANA_WSS_URL"),
                ).probe(timeout)
                return _observation_probe(provider, started, "SECONDARY", observation)
            if provider == "shyft":
                observation = await ShyftSolanaClient(
                    api_key=self.environment.get("SHYFT_API_KEY"),
                    rpc_url=self.environment.get("SHYFT_SOLANA_RPC_URL"),
                ).probe(timeout)
                return _observation_probe(provider, started, "FALLBACK", observation)
            if provider == "solscan":
                observation = await SolscanClient(
                    self.environment["SOLSCAN_API_KEY"],
                    base_url=self.environment.get(
                        "SOLSCAN_BASE_URL", "https://pro-api.solscan.io/v2.0"
                    ),
                ).probe(_PROBE_TOKEN, timeout)
                return _observation_probe(provider, started, "ENRICHMENT", observation)
            if provider == "coingecko":
                observation = await CoinGeckoContextClient(
                    api_key=self.environment.get("COINGECKO_API_KEY"),
                    base_url=self.environment.get(
                        "COINGECKO_BASE_URL", "https://api.coingecko.com/api/v3"
                    ),
                ).sol_context(timeout)
                return _observation_probe(provider, started, "CONTEXT", observation)
            if provider == "neynar_farcaster":
                evidence, latency = await NeynarFarcasterClient(
                    self.environment["NEYNAR_API_KEY"]
                ).search(_PROBE_TOKEN, "solana", timeout, limit=5)
                return ProviderProbe(
                    provider,
                    started,
                    _now(),
                    "ENRICHMENT",
                    events_seen=max(1, evidence.mentions),
                    matching_events=evidence.mentions,
                    tokens_seen=int(evidence.mentions > 0),
                    latencies_ms=(latency,),
                    coverage={"cast_search": True, "raw_content_persisted": False},
                    evidence={"credential_redacted": True, "schema_parsed": True},
                )
            if provider == "youtube":
                evidence, latencies = await YouTubeResearchClient(
                    self.environment["YOUTUBE_API_KEY"],
                    cache_ttl_seconds=float(
                        self.environment.get("YOUTUBE_CACHE_TTL_SECONDS", "21600")
                    ),
                    maximum_searches=1,
                ).search(_PROBE_TOKEN, "Solana", timeout, high_priority=True, max_results=3)
                assert evidence is not None
                return ProviderProbe(
                    provider,
                    started,
                    _now(),
                    "ENRICHMENT",
                    events_seen=max(1, evidence.mentions),
                    matching_events=evidence.mentions,
                    tokens_seen=int(evidence.mentions > 0),
                    latencies_ms=latencies,
                    coverage={"high_priority_search": True, "aggressive_cache": True},
                    evidence={"credential_redacted": True, "schema_parsed": True},
                )
            if provider == "telegram_public_web":
                channel = next(
                    value.strip().lstrip("@")
                    for value in (
                        self.environment.get("TELEGRAM_PUBLIC_CHANNELS")
                        or self.environment.get("TELEGRAM_CHANNELS")
                        or "telegramtips"
                    ).split(",")
                    if value.strip()
                )
                evidence, latency = await TelegramPublicWebClient().search_channel(
                    channel, _PROBE_TOKEN, "solana", timeout
                )
                return ProviderProbe(
                    provider,
                    started,
                    _now(),
                    "ENRICHMENT",
                    events_seen=max(1, evidence.mentions),
                    matching_events=evidence.mentions,
                    tokens_seen=int(evidence.mentions > 0),
                    latencies_ms=(latency,),
                    coverage={"public_preview": True, "private_access_attempted": False},
                )
            if provider == "mastodon":
                instances = tuple(
                    value.strip()
                    for value in (
                        self.environment.get("MASTODON_INSTANCE_URLS")
                        or self.environment.get("MASTODON_INSTANCE_URL")
                        or "https://mastodon.social"
                    ).split(",")
                    if value.strip()
                )
                evidence, latencies, errors = await MastodonPublicClient(
                    instances,
                    access_token=self.environment.get("MASTODON_ACCESS_TOKEN"),
                ).search(_PROBE_TOKEN, "solana", timeout)
                return ProviderProbe(
                    provider,
                    started,
                    _now(),
                    "ENRICHMENT",
                    events_seen=max(1, evidence.mentions),
                    matching_events=evidence.mentions,
                    tokens_seen=int(evidence.mentions > 0),
                    latencies_ms=latencies,
                    errors=tuple("instance unavailable" for _ in range(errors)),
                    coverage={"multi_instance_fallback": True, "raw_content_persisted": False},
                    evidence={"credential_redacted": True},
                )
            raise ValueError(f"no live probe for {provider}")
        except (
            TimeoutError,
            aiohttp.ClientError,
            ValueError,
            TypeError,
            KeyError,
            RuntimeError,
            StopIteration,
        ) as error:
            capability = next(item for item in capabilities() if item.provider == provider)
            credentialed = capability.credential_required or any(
                self.environment.get(name) for name in capability.credential_env
            )
            if credentialed and isinstance(error, aiohttp.ClientResponseError):
                detail = f"provider returned HTTP {error.status}; credential-bearing details redacted"
            elif credentialed:
                detail = "provider probe failed; credential-bearing details redacted"
            else:
                detail = str(error)[:240]
            return ProviderProbe(
                provider,
                started,
                _now(),
                "REJECTED",
                errors=(f"{type(error).__name__}: {detail}"[:300],),
            )

    async def _probe_solana(self, started: str, timeout: float) -> ProviderProbe:
        url = self.environment.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
        payload, latency = await _request_json(
            url,
            timeout,
            method="POST",
            body={"jsonrpc": "2.0", "id": 1, "method": "getSlot", "params": []},
        )
        slot = int(payload["result"])
        return ProviderProbe(
            "native_solana_rpc",
            started,
            _now(),
            "SECONDARY",
            events_seen=1,
            latencies_ms=(latency,),
            coverage={"mainnet_slot_observed": True},
            evidence={"slot": slot},
        )

    async def _probe_helius(self, started: str, timeout: float) -> ProviderProbe:
        key = self.environment["HELIUS_API_KEY"]
        url = f"https://mainnet.helius-rpc.com/?api-key={quote_plus(key)}"
        payload, latency = await _request_json(
            url,
            timeout,
            method="POST",
            body={"jsonrpc": "2.0", "id": 1, "method": "getSlot", "params": []},
        )
        return ProviderProbe(
            "helius",
            started,
            _now(),
            "PRIMARY" if payload.get("result") else "REJECTED",
            events_seen=int(bool(payload.get("result"))),
            latencies_ms=(latency,),
            coverage={"standard_rpc": True, "websocket_event_flow": False},
            evidence={"slot_observed": bool(payload.get("result")), "credential_redacted": True},
        )

    async def _probe_dexscreener(self, started: str, timeout: float) -> ProviderProbe:
        base = self.environment.get("DEXSCREENER_BASE_URL", "https://api.dexscreener.com").rstrip(
            "/"
        )
        paths = (
            "/token-profiles/latest/v1",
            "/token-boosts/latest/v1",
            "/community-takeovers/latest/v1",
            "/ads/latest/v1",
        )
        rows: list[dict[str, Any]] = []
        latencies = []
        counts = {}
        for path in paths:
            payload, latency = await _request_json(base + path, timeout)
            values = payload if isinstance(payload, list) else [payload]
            values = [value for value in values if isinstance(value, dict)]
            rows.extend(values)
            counts[path] = len(values)
            latencies.append(latency)
        tokens = {
            f"{row.get('chainId')}:{row.get('tokenAddress')}"
            for row in rows
            if row.get("tokenAddress")
        }
        return ProviderProbe(
            "dexscreener",
            started,
            _now(),
            "ENRICHMENT" if rows else "REJECTED",
            events_seen=len(rows),
            matching_events=len(rows),
            tokens_seen=len(tokens),
            latencies_ms=tuple(latencies),
            coverage={"endpoint_counts": counts, "promotional_not_runner_proof": True},
        )

    async def _probe_gecko(self, started: str, timeout: float) -> ProviderProbe:
        base = self.environment.get(
            "GECKOTERMINAL_BASE_URL", "https://api.geckoterminal.com/api/v2"
        ).rstrip("/")
        payload, latency = await _request_json(f"{base}/networks/solana/new_pools?page=1", timeout)
        rows = payload.get("data") or []
        return ProviderProbe(
            "geckoterminal",
            started,
            _now(),
            "SECONDARY" if rows else "REJECTED",
            events_seen=len(rows),
            tokens_seen=len(rows),
            latencies_ms=(latency,),
            coverage={"solana_new_pool_page": 1, "rolling_window_only": True},
        )

    async def _probe_bluesky(self, started: str, timeout: float) -> ProviderProbe:
        url = (
            "wss://jetstream.us-east.bsky.network/xrpc/"
            "network.bsky.jetstream.subscribeEvents?collections=app.bsky.feed.post&kinds=commit"
        )
        seen = matching = 0
        latency_values: list[float] = []
        begun = time.perf_counter()
        client_timeout = aiohttp.ClientTimeout(total=timeout)
        async with (
            aiohttp.ClientSession(timeout=client_timeout) as session,
            session.ws_connect(
                url, protocols=("xrpc.v1.json",), heartbeat=20, autoping=True
            ) as websocket,
        ):
            deadline = time.monotonic() + timeout
            while seen < 5 and time.monotonic() < deadline:
                remaining = max(0.1, deadline - time.monotonic())
                message = await asyncio.wait_for(websocket.receive(), timeout=remaining)
                if message.type != aiohttp.WSMsgType.TEXT:
                    continue
                payload = json.loads(message.data).get("payload") or {}
                record = payload.get("record") or {}
                text = str(record.get("text") or "")
                seen += 1
                matching += int("pump" in text.lower() or "solana" in text.lower())
                latency_values.append((time.perf_counter() - begun) * 1000)
        return ProviderProbe(
            "bluesky_jetstream",
            started,
            _now(),
            "ENRICHMENT" if seen else "REJECTED",
            events_seen=seen,
            matching_events=matching,
            latencies_ms=tuple(latency_values),
            coverage={"collection": "app.bsky.feed.post", "content_persisted": False},
        )

    async def _probe_pumpportal(self, started: str, timeout: float) -> ProviderProbe:
        base = self.environment.get("PUMPPORTAL_WEBSOCKET_URL", "wss://pumpportal.fun/api/data")
        url = f"{base}?api-key={quote_plus(self.environment['PUMPPORTAL_API_KEY'])}"
        client_timeout = aiohttp.ClientTimeout(total=timeout)
        begun = time.perf_counter()
        async with (
            aiohttp.ClientSession(timeout=client_timeout) as session,
            session.ws_connect(url, heartbeat=20, autoping=True) as websocket,
        ):
            await websocket.send_json({"method": "subscribeNewToken"})
            await websocket.send_json({"method": "subscribeMigration"})
            message = await asyncio.wait_for(websocket.receive(), timeout=timeout)
            if message.type != aiohttp.WSMsgType.TEXT:
                raise ValueError(f"unexpected websocket message {message.type}")
            payload = json.loads(message.data)
        event = bool(payload.get("mint") or payload.get("token"))
        return ProviderProbe(
            "pumpportal",
            started,
            _now(),
            "SECONDARY" if event else "RETRYABLE_FAILURE",
            events_seen=int(event),
            tokens_seen=int(event),
            latencies_ms=((time.perf_counter() - begun) * 1000,),
            coverage={"new_token": True, "migration": True, "trade_stream_used": False},
            evidence={"credential_redacted": True},
        )

    async def _probe_dune(self, started: str, timeout: float) -> ProviderProbe:
        begun = time.perf_counter()
        client = DuneExecutionClient(
            self.environment["DUNE_API_KEY"],
            timeout_seconds=timeout,
            maximum_polls=max(2, round(timeout / 2)),
        )
        execution_id = await client.execute_sql("SELECT 1 AS gambit_direct_sql_probe")
        await client.wait(execution_id)
        payload = await client.results(execution_id, 0, 1)
        latency = (time.perf_counter() - begun) * 1000
        return ProviderProbe(
            "dune",
            started,
            _now(),
            "HISTORICAL_BACKBONE" if payload else "REJECTED",
            events_seen=int(bool(payload)),
            latencies_ms=(latency,),
            coverage={"direct_sql_executed": True, "historical_rows_ingested": False},
            evidence={"execution_id": execution_id, "credential_redacted": True},
        )

    def _persist_probe(self, probe: ProviderProbe) -> dict[str, Any]:
        values = probe.persisted()
        with self.store._lock, self.store.conn:
            self.store.conn.execute(
                "INSERT INTO provider_probes_v15(probe_id,provider,started_at,completed_at,state,"
                "events_seen,matching_events,tokens_seen,latency_p50_ms,latency_p95_ms,error_count,"
                "coverage_json,evidence_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    values["probe_id"],
                    probe.provider,
                    probe.started_at,
                    probe.completed_at,
                    probe.state,
                    probe.events_seen,
                    probe.matching_events,
                    probe.tokens_seen,
                    values["latency_p50_ms"],
                    values["latency_p95_ms"],
                    values["error_count"],
                    _json(probe.coverage),
                    _json(probe.evidence),
                ),
            )
            self.store.conn.execute(
                "UPDATE provider_capabilities_v15 SET admission_state=?,updated_at=? WHERE provider=?",
                (probe.state, _now(), probe.provider),
            )
        capability = next(item for item in capabilities() if item.provider == probe.provider)
        return {
            "provider": probe.provider,
            "configured": capability.configured(self.environment),
            "admission_state": probe.state,
            "live_state": probe.state,
            "state": probe.state,
            "events_seen": probe.events_seen,
            "events": probe.events_seen,
            "matching_events": probe.matching_events,
            "tokens_seen": probe.tokens_seen,
            "tokens": probe.tokens_seen,
            "latency_p50_ms": values["latency_p50_ms"],
            "latency_p95_ms": values["latency_p95_ms"],
            "error_count": values["error_count"],
            "errors": list(probe.errors),
            "role": capability.production_role,
            "capability_role": capability.role,
            "coverage": probe.coverage,
        }

    def status(self) -> list[dict[str, Any]]:
        rows = self.store.conn.execute(
            "SELECT c.*,p.state live_state,p.events_seen,p.tokens_seen,p.latency_p50_ms,"
            "p.latency_p95_ms,p.error_count,p.completed_at last_probe_at FROM "
            "provider_capabilities_v15 c LEFT JOIN provider_probes_v15 p ON p.probe_id=("
            "SELECT probe_id FROM provider_probes_v15 newer WHERE newer.provider=c.provider "
            "ORDER BY newer.completed_at DESC LIMIT 1) ORDER BY c.provider"
        )
        output = []
        for row in rows:
            item = dict(row)
            for key in (
                "credential_env_json",
                "capabilities_json",
                "rate_limit_json",
                "cost_json",
            ):
                item[key.removesuffix("_json")] = json.loads(item.pop(key))
            output.append(item)
        return output


async def _request_json(
    url: str,
    timeout: float,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[Any, float]:
    begun = time.perf_counter()
    client_timeout = aiohttp.ClientTimeout(total=timeout)
    async with (
        aiohttp.ClientSession(timeout=client_timeout) as session,
        session.request(method, url, json=body, headers=headers) as response,
    ):
        response.raise_for_status()
        payload = await response.json()
    return payload, (time.perf_counter() - begun) * 1000


def _percentile(values: tuple[float, ...], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * quantile)))
    return ordered[index]


def _observation_probe(
    provider: str,
    started_at: str,
    state: str,
    observation: ProviderObservation,
) -> ProviderProbe:
    return ProviderProbe(
        provider,
        started_at,
        _now(),
        state,
        events_seen=observation.events_seen,
        matching_events=observation.events_seen,
        tokens_seen=observation.tokens_seen,
        latencies_ms=observation.latencies_ms,
        coverage=observation.coverage,
        evidence=observation.evidence,
        errors=observation.errors,
    )


def _json(value: Any) -> str:
    return json.dumps(value, default=str, separators=(",", ":"), sort_keys=True)
