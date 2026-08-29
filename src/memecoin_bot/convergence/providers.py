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

DOCS_CHECKED_AT = "2026-08-29"


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

    def configured(self, environment: dict[str, str] | None = None) -> bool:
        values = environment if environment is not None else os.environ
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
            raise ValueError(f"no live probe for {provider}")
        except (TimeoutError, aiohttp.ClientError, ValueError, KeyError) as error:
            credentialed = next(
                item.credential_required for item in capabilities() if item.provider == provider
            )
            detail = (
                "provider probe failed; credential-bearing details redacted"
                if credentialed
                else str(error)[:240]
            )
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
        return {
            "provider": probe.provider,
            "state": probe.state,
            "events": probe.events_seen,
            "matching_events": probe.matching_events,
            "tokens": probe.tokens_seen,
            "latency_p50_ms": values["latency_p50_ms"],
            "latency_p95_ms": values["latency_p95_ms"],
            "errors": list(probe.errors),
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


def _json(value: Any) -> str:
    return json.dumps(value, default=str, separators=(",", ":"), sort_keys=True)
