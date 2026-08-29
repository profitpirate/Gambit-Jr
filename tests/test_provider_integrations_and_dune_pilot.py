from __future__ import annotations

import json
from pathlib import Path
from typing import ClassVar

import aiohttp
import pytest

from memecoin_bot.config import Settings
from memecoin_bot.convergence.providers import ProviderRegistry
from memecoin_bot.historical.backfill import BackfillPage
from memecoin_bot.historical.dune_pilot import (
    DuneAcquisitionConfig,
    DunePilotRunner,
    build_dune_plan,
)
from memecoin_bot.historical.providers import (
    DuneMonthHistoricalProvider,
    DuneParquetPartitionWriter,
)
from memecoin_bot.historical.store import HistoricalWarehouse, RawEvidence
from memecoin_bot.providers import external
from memecoin_bot.providers.external import (
    AlchemySolanaClient,
    BirdeyeClient,
    CoinGeckoContextClient,
    ShyftSolanaClient,
    SolanaTrackerClient,
    SolscanClient,
)
from memecoin_bot.social.evidence import (
    SocialEvidence,
    SocialEvidenceStore,
    SocialLinkClass,
    classify_social_link,
    fuse_social_evidence,
    social_research_features,
)
from memecoin_bot.social.public_sources import (
    MastodonPublicClient,
    NeynarFarcasterClient,
    PublicPreviewUnavailable,
    TelegramPublicWebClient,
    YouTubeResearchClient,
)

TOKEN = "So11111111111111111111111111111111111111112"
SIGNATURE = "5" * 64
NOW = "2026-07-15T12:00:00+00:00"


@pytest.fixture
def warehouse(tmp_path):
    value = HistoricalWarehouse(tmp_path / "warehouse.db", tmp_path / "archive")
    try:
        yield value
    finally:
        value.close()


def test_capability_admission_covers_new_providers_and_alternatives(warehouse):
    registry = ProviderRegistry(
        warehouse,
        {
            "BIRDEYE_API_KEY": "secret",
            "SOLANA_TRACKER_API_KEY": "secret",
            "ALCHEMY_SOLANA_RPC_URL": "https://rpc.example/v2/secret",
            "SHYFT_SOLANA_RPC_URL": "https://rpc.example/?api_key=secret",
            "SOLSCAN_API_KEY": "secret",
            "NEYNAR_API_KEY": "secret",
            "YOUTUBE_API_KEY": "secret",
            "TELEGRAM_PUBLIC_CHANNELS": "public_one",
            "MASTODON_INSTANCE_URLS": "https://one.example,https://two.example",
        },
    )
    rows = {row["provider"]: row for row in registry.refresh()}
    for provider in (
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
    ):
        assert rows[provider]["configured"] is True
    assert ProviderRegistry(warehouse, {}).refresh()
    unconfigured = {row["provider"]: row for row in ProviderRegistry(warehouse, {}).refresh()}
    assert unconfigured["birdeye"]["configured"] is False
    assert unconfigured["coingecko"]["configured"] is True


def test_configuration_refresh_preserves_a_live_admission_state(warehouse):
    environment = {"BIRDEYE_API_KEY": "secret"}
    registry = ProviderRegistry(warehouse, environment)
    registry.refresh()
    with warehouse.conn:
        warehouse.conn.execute(
            "UPDATE provider_capabilities_v15 SET admission_state='ENRICHMENT' "
            "WHERE provider='birdeye'"
        )
    refreshed = {row["provider"]: row for row in registry.refresh()}
    assert refreshed["birdeye"]["admission_state"] == "ENRICHMENT"


@pytest.mark.asyncio
async def test_birdeye_holder_profile_parses_only_available_plan_fields():
    calls = []

    async def request(url, timeout, **kwargs):
        calls.append((url, timeout, kwargs))
        return {"data": {"holder_count": 12, "sniper_wallet_count": 2}}, 4.5

    result = await BirdeyeClient("secret", requester=request).holder_profile(TOKEN, 2)
    assert result.tokens_seen == 1
    assert result.coverage["holder_count_present"] is True
    assert result.coverage["label_fields_present"] == ["sniper_wallet_count"]
    assert calls[0][2]["headers"]["X-API-KEY"] == "secret"


@pytest.mark.asyncio
async def test_solana_tracker_rpc_websocket_and_indexed_paths(monkeypatch):
    calls = []

    async def request(url, timeout, **kwargs):
        calls.append((url, kwargs))
        if kwargs.get("method") == "POST":
            return {"result": 123}, 2.0
        return {"price": 99}, 3.0

    async def websocket(url, timeout):
        assert "tracker-secret" in url
        return 124, 4.0

    monkeypatch.setattr(external, "websocket_slot", websocket)
    client = SolanaTrackerClient("tracker-secret", requester=request)
    result = await client.probe(TOKEN, 2)
    assert result.events_seen == 4
    assert result.coverage["indexed_token_lookup"] is True
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_alchemy_key_derivation_and_explicit_override(monkeypatch):
    settings = Settings(alchemy_api_key="key one")
    assert settings.effective_alchemy_rpc_url().endswith("/key+one")
    assert settings.effective_alchemy_websocket_url().endswith("/key+one")
    explicit = Settings(
        alchemy_api_key="ignored",
        alchemy_solana_rpc_url="https://explicit.example/rpc",
        alchemy_solana_wss_url="wss://explicit.example/wss",
    )
    assert explicit.effective_alchemy_rpc_url() == "https://explicit.example/rpc"
    assert explicit.effective_alchemy_websocket_url() == "wss://explicit.example/wss"

    async def request(url, timeout, **kwargs):
        assert url == "https://explicit.example/rpc"
        return {"result": 999}, 1.0

    async def websocket(url, timeout):
        assert url == "wss://explicit.example/wss"
        return 1000, 2.0

    monkeypatch.setattr(external, "websocket_slot", websocket)
    result = await AlchemySolanaClient(
        api_key="ignored",
        rpc_url="https://explicit.example/rpc",
        wss_url="wss://explicit.example/wss",
        requester=request,
    ).probe(3)
    assert result.coverage["websocket_slot_subscription"] is True


@pytest.mark.asyncio
async def test_shyft_solscan_and_coingecko_parse_bounded_requests():
    async def rpc_request(url, timeout, **kwargs):
        return {"result": 101}, 1.5

    shyft = await ShyftSolanaClient(api_key="secret", requester=rpc_request).probe(2)
    assert shyft.coverage == {"rpc_get_slot": True, "fallback_only": True}

    async def solscan_request(url, timeout, **kwargs):
        assert "page_size=10" in url
        return {"data": {"items": [{"owner": "wallet"}]}}, 2.5

    solscan = await SolscanClient("secret", requester=solscan_request).token_holders(TOKEN, 2)
    assert solscan.events_seen == 1

    async def gecko_request(url, timeout, **kwargs):
        return {"solana": {"usd": 150, "usd_24h_change": 3.2}}, 3.5

    gecko = await CoinGeckoContextClient(
        api_key="demo", requester=gecko_request
    ).sol_context(2)
    assert gecko.coverage["slow_context_only"] is True


@pytest.mark.asyncio
async def test_neynar_parse_and_youtube_high_priority_cache():
    async def neynar_request(url, timeout, **kwargs):
        return {
            "result": {
                "casts": [
                    {
                        "timestamp": NOW,
                        "author": {"fid": 42},
                        "reactions": {"likes_count": 2, "recasts_count": 1},
                        "replies": {"count": 3},
                    }
                ]
            }
        }, 5.0

    evidence, latency = await NeynarFarcasterClient(
        "secret", requester=neynar_request
    ).search(TOKEN, TOKEN, 2)
    assert evidence.mentions == 1 and evidence.engagement == 6
    assert latency == 5.0

    calls = []

    async def youtube_request(url, timeout, **kwargs):
        calls.append(url)
        if "/search?" in url:
            return {
                "items": [
                    {
                        "id": {"videoId": "video-1"},
                        "snippet": {"publishedAt": NOW, "channelId": "channel-1"},
                    }
                ]
            }, 6.0
        return {
            "items": [
                {
                    "id": "video-1",
                    "statistics": {"viewCount": "100", "commentCount": "5"},
                }
            ]
        }, 7.0

    youtube = YouTubeResearchClient("secret", requester=youtube_request, maximum_searches=1)
    assert await youtube.search(TOKEN, TOKEN, 2, high_priority=False) == (None, ())
    first, _ = await youtube.search(TOKEN, TOKEN, 2, high_priority=True)
    second, _ = await youtube.search(TOKEN, TOKEN, 2, high_priority=True)
    assert first == second
    assert first and first.provenance["view_count"] == 100
    assert len(calls) == 2


def test_telegram_public_preview_parsing_and_unavailable_state():
    page = f'''
    <div class="tgme_widget_message_wrap">
      <div class="tgme_widget_message" data-post="public/1">
      <div class="tgme_widget_message_text">watch {TOKEN}</div>
      <time datetime="{NOW}"></time><span class="tgme_widget_message_views">1.2K</span>
      </div>
    </div>
    '''
    evidence = TelegramPublicWebClient.parse_public_preview(page, "public", TOKEN, TOKEN)
    assert evidence.mentions == 1
    assert evidence.engagement == 1200
    assert evidence.community_profile_class == SocialLinkClass.COMMUNITY
    with pytest.raises(PublicPreviewUnavailable):
        TelegramPublicWebClient.parse_public_preview("<html>private</html>", "x", TOKEN, TOKEN)


@pytest.mark.asyncio
async def test_mastodon_multi_instance_fallback():
    calls = []

    async def request(url, timeout, **kwargs):
        calls.append(url)
        if "one.example" in url:
            raise aiohttp.ClientConnectionError("unavailable")
        return {
            "statuses": [
                {
                    "created_at": NOW,
                    "account": {"id": "account-1"},
                    "reblogs_count": 1,
                    "favourites_count": 2,
                    "replies_count": 3,
                }
            ]
        }, 8.0

    evidence, latencies, errors = await MastodonPublicClient(
        ["https://one.example", "https://two.example"], requester=request
    ).search(TOKEN, TOKEN, 2)
    assert evidence.mentions == 1
    assert latencies == (8.0,)
    assert errors == 1 and len(calls) == 2


def test_social_classification_fusion_features_and_persistence(warehouse):
    community = classify_social_link(TOKEN, "telegram", "https://t.me/token_chat", NOW)
    profile = classify_social_link(
        TOKEN,
        "youtube",
        "https://youtube.com/@creator",
        NOW,
        metadata={"kind": "creator profile"},
    )
    assert community.classification == SocialLinkClass.COMMUNITY
    assert profile.classification == SocialLinkClass.PROFILE
    base = SocialEvidence(
        TOKEN,
        "telegram",
        "public_web",
        NOW,
        authors=("a", "b"),
        mentions=2,
        acceleration=1.5,
        first_seen="2026-07-15T11:00:00+00:00",
        community_profile_class=SocialLinkClass.COMMUNITY,
        provenance={"canonical_content_sha256": "same", "point_in_time": True},
    )
    mirror = SocialEvidence(
        TOKEN,
        "mastodon",
        "public_search",
        NOW,
        authors=("c",),
        mentions=1,
        community_profile_class=SocialLinkClass.UNKNOWN,
        provenance={"canonical_content_sha256": "same", "point_in_time": True},
    )
    future = SocialEvidence(
        TOKEN,
        "youtube",
        "search",
        "2026-07-16T12:00:00+00:00",
        mentions=1,
    )
    assert fuse_social_evidence([base, mirror, future], as_of=NOW) == [base]
    features = social_research_features(
        [base, mirror, future],
        as_of=NOW,
        price_move_at="2026-07-15T11:30:00+00:00",
        first_sell_at="2026-07-15T11:30:00+00:00",
    )
    assert features["community_linked"] is True
    assert features["profile_linked"] is False
    assert features["social_before_price_move"] is True
    assert features["hard_coded_weight"] is None
    store = SocialEvidenceStore(warehouse)
    assert store.persist_evidence(base)[1] is True
    assert store.persist_evidence(base)[1] is False
    assert store.persist_link(community)[1] is True


def test_dune_dry_run_requires_explicit_range_and_never_executes(warehouse):
    default = DuneAcquisitionConfig()
    plan = build_dune_plan(warehouse, default)
    assert plan["execution_performed"] is False
    assert plan["planned_executions"] == 0
    assert plan["reason"] == "EXPLICIT_DUNE_MONTH_RANGE_REQUIRED"
    bounded = DuneAcquisitionConfig(
        "2026-07",
        "2026-07",
        4,
        True,
    )
    bounded_plan = build_dune_plan(warehouse, bounded)
    assert bounded_plan["planned_executions"] == 4
    assert bounded_plan["estimated_partitions"] == 4

    warehouse.record_dune_partition(
        {
            "query_name": "monthly_universe",
            "schema_version": "1.0.0",
            "month": "2026-07",
            "execution_id": "sample-execution",
            "offset": 10_000,
            "total_rows": 10_000,
            "source_total_rows": 1_000_000,
            "partial_results": True,
            "materialization_mode": "BOUNDED_SERVER_SAMPLE",
            "state": "PILOT_SAMPLE_COMPLETE",
        }
    )
    assert build_dune_plan(warehouse, bounded)["planned_executions"] == 3
    full_materialization = DuneAcquisitionConfig(
        "2026-07",
        "2026-07",
        4,
        True,
        pilot_sample_rows=0,
    )
    assert build_dune_plan(warehouse, full_materialization)["planned_executions"] == 4


@pytest.mark.asyncio
async def test_large_dune_pilot_uses_bounded_sample_without_marking_full_partition(tmp_path):
    class SampleClient:
        async def wait(self, execution_id):
            assert execution_id == "existing-execution"
            return {
                "execution_cost_credits": 12.5,
                "result_metadata": {
                    "total_row_count": 1_000_000,
                    "total_result_set_bytes": 250_000_000,
                },
            }

        async def sample_results(self, execution_id, sample_count):
            assert execution_id == "existing-execution"
            assert sample_count == 1
            return {"result": {"rows": [_payload("pumpfun_trades")]}}

        async def results(self, execution_id, offset, limit):
            raise AssertionError("a large pilot must not begin a full result export")

    provider = DuneMonthHistoricalProvider(
        None,
        "2026-07",
        None,
        query_name="pumpfun_trades",
        client=SampleClient(),
        parquet_root=tmp_path / "sample",
        materialize_raw_records=False,
        maximum_result_rows=1,
    )
    page = await provider.fetch_page(
        {
            "execution_id": "existing-execution",
            "execution_mode": "DIRECT_SQL",
            "offset": 0,
        }
    )
    partition = page.metadata["dune_partition"]
    assert page.next_cursor is None
    assert partition["state"] == "PILOT_SAMPLE_COMPLETE"
    assert partition["total_rows"] == 1
    assert partition["source_total_rows"] == 1_000_000
    assert partition["source_result_bytes"] == 250_000_000
    assert provider.last_result_metadata["materialization_mode"] == "BOUNDED_SERVER_SAMPLE"


class FakePilotProvider:
    name = "dune_month_partition"
    instances: ClassVar[list[FakePilotProvider]] = []
    failures_remaining = 0
    always_fail_instances = 0

    def __init__(
        self,
        query_id,
        month,
        api_key,
        *,
        query_name,
        registry,
        parquet_root,
        **kwargs,
    ):
        self.query_name = query_name
        self.query_spec = registry.spec(query_name)
        self.month = month
        self.dataset_id = f"dune-{query_name}-{month}"
        self.parquet_root = Path(parquet_root)
        self.page_size = kwargs.get("page_size")
        self.recovery_cursor = None
        self.last_execution_metadata = {"credits_used": 1.25}
        self.checkpoint = None
        self.received_cursors = []
        self.always_fail = self.__class__.always_fail_instances > 0
        if self.always_fail:
            self.__class__.always_fail_instances -= 1
        self.__class__.instances.append(self)

    def bind_checkpoint(self, callback):
        self.checkpoint = callback

    async def fetch_page(self, cursor):
        self.received_cursors.append(cursor)
        execution_id = (cursor or {}).get("execution_id") or f"execution-{self.query_name}"
        self.recovery_cursor = {
            "execution_id": execution_id,
            "offset": 0,
            "execution_mode": "DIRECT_SQL",
        }
        if self.checkpoint:
            self.checkpoint(self.recovery_cursor)
        if self.always_fail or self.__class__.failures_remaining > 0:
            if self.__class__.failures_remaining:
                self.__class__.failures_remaining -= 1
            raise RuntimeError("controlled fixture failure")
        observed_at = f"{self.month}-15T12:00:00+00:00"
        payload = _payload(self.query_name, observed_at)
        evidence = RawEvidence(
            dataset_id=self.dataset_id,
            provider=self.name,
            chain="solana",
            entity_type="pumpfun_launch_evidence",
            entity_id=TOKEN,
            source_timestamp=observed_at,
            availability_timestamp="2026-08-01T00:00:00+00:00",
            endpoint_type="dune_direct_sql_month_result",
            payload=payload,
            schema_version="test",
            acquisition_version="test",
            provenance={
                "query_name": self.query_name,
                "execution_id": execution_id,
                "partial_results_allowed": False,
            },
        )
        partition = (
            self.parquet_root
            / self.query_name
            / f"year={self.month[:4]}"
            / f"month={self.month[5:7]}"
        )
        partition.mkdir(parents=True, exist_ok=True)
        DuneParquetPartitionWriter(self.parquet_root).write(
            self.query_name, self.month, 0, [payload]
        )
        self.recovery_cursor = None
        return BackfillPage(
            [evidence],
            None,
            0,
            metadata={
                "dune_partition": {
                    "query_name": self.query_name,
                    "schema_version": self.query_spec.schema_version,
                    "month": self.month,
                    "execution_id": execution_id,
                    "offset": 1,
                    "total_rows": 1,
                    "state": "COMPLETE",
                }
            },
        )


def _payload(query_name, observed_at=NOW):
    base = {
        "token_address": TOKEN,
        "observed_at": observed_at,
        "tx_id": SIGNATURE,
        "block_slot": 1,
    }
    if query_name in {"monthly_universe", "pumpfun_launches"}:
        return {**base, "creator": TOKEN, "source": "pumpfun_transaction"}
    if query_name == "pumpfun_trades":
        return {
            **base,
            "trader": TOKEN,
            "side": "buy",
            "token_amount": 10,
            "amount_usd": 2,
            "source": "dex_solana.trades:pumpfun",
        }
    return {
        **base,
        "price_usd": 0.2,
        "amount_usd": 2,
        "source": "dex_solana.trades:outcome_path",
    }


@pytest.mark.asyncio
async def test_dune_execution_cap_pilot_semantics_and_completed_skip(warehouse, tmp_path):
    FakePilotProvider.instances = []
    config = DuneAcquisitionConfig(
        "2026-07",
        "2026-07",
        4,
        False,
        parquet_root=tmp_path / "parquet",
    )
    runner = DunePilotRunner(
        warehouse,
        "secret",
        config,
        provider_factory=FakePilotProvider,
    )
    result = await runner.run(execute=True)
    assert result["state"] == "COMPLETE"
    assert result["executions_started"] == 4
    assert result["schema_validation"] == "PASS"
    assert result["semantic_validation"] == "PASS"
    assert result["total_rows"] == 4
    assert result["total_source_rows"] == 4
    assert result["sampled_partitions"] == 0
    assert result["credits_used"] == 5.0
    assert all(provider.page_size == 32_000 for provider in FakePilotProvider.instances)
    assert runner.plan()["planned_executions"] == 0
    assert len(runner.plan()["existing_completed_partitions"]) == 4

    second_month = DuneAcquisitionConfig(
        "2026-06",
        "2026-07",
        2,
        False,
        parquet_root=tmp_path / "second",
    )
    capped = await DunePilotRunner(
        warehouse,
        "secret",
        second_month,
        provider_factory=FakePilotProvider,
    ).run(execute=True)
    assert capped["state"] == "PARTIAL"
    assert capped["executions_started"] == 2
    assert capped["partitions_remaining_after_cap"] == 2


@pytest.mark.asyncio
async def test_dune_resume_after_failure_reuses_persisted_execution(warehouse, tmp_path):
    FakePilotProvider.instances = []
    FakePilotProvider.always_fail_instances = 1
    config = DuneAcquisitionConfig(
        "2026-07",
        "2026-07",
        1,
        False,
        query_names=("monthly_universe",),
        parquet_root=tmp_path / "resume",
    )
    first = await DunePilotRunner(
        warehouse,
        "secret",
        config,
        provider_factory=FakePilotProvider,
    ).run(execute=True)
    assert first["state"] == "FAILED"
    second = await DunePilotRunner(
        warehouse,
        "secret",
        config,
        provider_factory=FakePilotProvider,
    ).run(execute=True)
    assert second["state"] == "COMPLETE"
    resumed = FakePilotProvider.instances[-1].received_cursors[0]
    assert resumed["execution_id"] == "execution-monthly_universe"


def test_probe_error_redaction_covers_embedded_rpc_url(warehouse, monkeypatch):
    secret = "alchemy-super-secret"

    async def fail_probe(self, timeout):
        raise ValueError(f"https://rpc.example/v2/{secret}")

    monkeypatch.setattr(AlchemySolanaClient, "probe", fail_probe)
    registry = ProviderRegistry(
        warehouse, {"ALCHEMY_SOLANA_RPC_URL": f"https://rpc.example/v2/{secret}"}
    )
    result = __import__("asyncio").run(registry.probe({"alchemy"}))
    dumped = json.dumps(result)
    assert secret not in dumped
    assert result[0]["error_count"] == 1
    assert result[0]["role"]
