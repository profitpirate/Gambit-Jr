from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, content: str) -> None:
    (ROOT / path).write_text(content, encoding="utf-8")


def replace_once(path: str, old: str, new: str) -> None:
    content = read(path)
    count = content.count(old)
    if count != 1:
        raise RuntimeError(f"{path}: expected one exact match, found {count}: {old[:120]!r}")
    write(path, content.replace(old, new, 1))


def replace_regex(path: str, pattern: str, replacement: str) -> None:
    content = read(path)
    updated, count = re.subn(pattern, replacement, content, count=1, flags=re.DOTALL)
    if count != 1:
        raise RuntimeError(f"{path}: expected one regex match, found {count}: {pattern[:120]!r}")
    write(path, updated)


# ---------------------------------------------------------------------------
# Discord: acknowledge every command before work, bound command execution,
# keep successful cards successful even if optional audit persistence fails.
# ---------------------------------------------------------------------------
BOT = "src/memecoin_bot/discord/bot_runtime.py"
replace_once(
    BOT,
    'DEFERRED_COMMANDS = frozenset({"compare", "scan"})\n',
    'DEFERRED_COMMANDS = EXPECTED_COMMAND_NAMES\n'
    'COMMAND_ACK_TIMEOUT_SECONDS = 2.5\n'
    'COMMAND_TIMEOUT_SECONDS = 30.0\n'
    'SAFE_TIMEOUT_ERROR = (\n'
    '    "Gambit Jr acknowledged the command, but the operation exceeded its safe time limit. "\n'
    '    "Please try again."\n'
    ')\n',
)

replace_regex(
    BOT,
    r"    def track_command\(callback: CommandCallback\) -> CommandCallback:\n.*?"
    r"        return tracked  # type: ignore\[return-value\]\n\n",
    '''    def track_command(callback: CommandCallback) -> CommandCallback:
        @functools.wraps(callback)
        async def tracked(interaction: discord.Interaction, *args: Any, **kwargs: Any) -> None:
            started = time.monotonic()
            name = callback.__name__.removesuffix("_command").replace("_", "-")
            active_command_names[id(interaction)] = name
            event(
                log,
                logging.INFO,
                "command_received",
                command_name=name,
                guild_id=interaction.guild_id,
                channel_id=interaction.channel_id,
                user_id=getattr(interaction.user, "id", None),
            )
            session: InteractionResponder | None = None
            try:
                # Acknowledge at the outermost command boundary. No command is
                # allowed to touch SQLite, providers, rendering, or business logic
                # before Discord receives its acknowledgement.
                if command_allowed(interaction):
                    visibility = (
                        ResponseVisibility.PRIVATE
                        if name in PRIVATE_COMMANDS
                        else ResponseVisibility.PUBLIC
                    )
                    session = InteractionResponder(interaction, name, visibility, log)
                    response_sessions[id(interaction)] = session
                    await asyncio.wait_for(
                        session.defer(), timeout=COMMAND_ACK_TIMEOUT_SECONDS
                    )
                await asyncio.wait_for(
                    callback(interaction, *args, **kwargs),
                    timeout=COMMAND_TIMEOUT_SECONDS,
                )
            except TimeoutError as error:
                _safe_failure_log(
                    log,
                    "command_timeout",
                    error,
                    command_name=name,
                    duration_ms=round((time.monotonic() - started) * 1000, 1),
                    result="timeout",
                )
                if session is None or not session.primary_completed:
                    await respond_command_error(interaction, SAFE_TIMEOUT_ERROR)
                return
            except Exception as error:  # noqa: BLE001 - command boundary must contain failures
                _safe_failure_log(
                    log,
                    "command_completed",
                    error,
                    command_name=name,
                    duration_ms=round((time.monotonic() - started) * 1000, 1),
                    result="failure",
                )
                if session is None or not session.primary_completed:
                    await respond_command_error(interaction, SAFE_INTERNAL_ERROR)
                return
            finally:
                response_sessions.pop(id(interaction), None)
                active_command_names.pop(id(interaction), None)
            event(
                log,
                logging.INFO,
                "command_completed",
                command_name=name,
                duration_ms=round((time.monotonic() - started) * 1000, 1),
                result="success",
            )

        return tracked  # type: ignore[return-value]

''',
)

replace_regex(
    BOT,
    r"    async def require_guild\(interaction: discord\.Interaction\) -> bool:\n.*?"
    r"        return False\n\n",
    '''    async def require_guild(interaction: discord.Interaction) -> bool:
        if command_allowed(interaction):
            # The outer command wrapper normally creates and acknowledges this
            # session. Keep a defensive fallback for direct callback tests and
            # future command registration paths.
            if id(interaction) not in response_sessions:
                command = active_command_names.get(id(interaction)) or getattr(
                    getattr(interaction, "command", None), "name", "unknown"
                )
                visibility = (
                    ResponseVisibility.PRIVATE
                    if command in PRIVATE_COMMANDS
                    else ResponseVisibility.PUBLIC
                )
                session = InteractionResponder(interaction, command, visibility, log)
                response_sessions[id(interaction)] = session
                await asyncio.wait_for(
                    session.defer(), timeout=COMMAND_ACK_TIMEOUT_SECONDS
                )
            return True
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "This command is available in Discord server text channels.", ephemeral=True
            )
        return False

''',
)

replace_once(
    BOT,
    '''        message = await send_card(interaction, test_alert_card())
        if message is None:
            message = await interaction.original_response()
        store.record_test_alert(
            interaction.guild_id, interaction.channel_id, interaction.user.id, str(message.id)
        )
''',
    '''        message = await send_card(interaction, test_alert_card())
        remote_id = str(message.id) if message is not None else None
        try:
            await asyncio.to_thread(
                store.record_test_alert,
                interaction.guild_id,
                interaction.channel_id,
                interaction.user.id,
                remote_id,
            )
        except Exception as error:  # noqa: BLE001 - optional audit must not fail delivery
            _safe_failure_log(
                log,
                "test_alert_audit_failed",
                error,
                command_name="test-alert",
                guild_id=interaction.guild_id,
                channel_id=interaction.channel_id,
                result="audit_failure_card_succeeded",
            )
''',
)

replace_once(
    BOT,
    '''                stats["model"] = operator_model_status(settings)
            await send_card(interaction, status_card(stats))
''',
    '''                stats["model"] = operator_model_status(settings)
            runtime_health = getattr(service, "runtime_health", None)
            stats["runtime"] = runtime_health() if callable(runtime_health) else {}
            await send_card(interaction, status_card(stats))
''',
)

replace_once(
    BOT,
    '''                lambda r: (
                    f"**{r.get('name') or r.get('symbol')}** • {r.get('chain')} • {r.get('state')} • score {float(r.get('normalized_score') or 0):.1f}"
                ),
''',
    '''                lambda r: (
                    f"**{r.get('name') or r.get('symbol') or 'UNKNOWN'}** • "
                    f"{r.get('chain') or 'UNKNOWN'} • "
                    f"{(str(r.get('decision_tier')) + ' call') if r.get('route_state') in {'OPERATOR_SHADOW_ALERT', 'PUBLIC_ALERT'} else 'developing setup'} • "
                    f"{r.get('decision_reason') or r.get('reason') or 'evidence pending'}"
                ),
''',
)

# Remove the exact Unicode component that Discord rejected in production.
COMMAND_CENTER = "src/memecoin_bot/discord/command_center.py"
replace_once(COMMAND_CENTER, '        emoji="↻",\n', "")
replace_once(
    COMMAND_CENTER,
    '''        stats["model"] = operator_model_status(self.settings)
        payload = status_card(stats)
''',
    '''        stats["model"] = operator_model_status(self.settings)
        runtime_health = getattr(self.service, "runtime_health", None)
        stats["runtime"] = runtime_health() if callable(runtime_health) else {}
        payload = status_card(stats)
''',
)

# ---------------------------------------------------------------------------
# V1.5: low-coverage partial evidence cannot become a routable STRONG call.
# ---------------------------------------------------------------------------
V15 = "src/memecoin_bot/v15_engine.py"
replace_once(
    V15,
    '''    if entry in {EntryStatus.CHASING, EntryStatus.CLOSED, EntryStatus.UNKNOWN} and tier in {
        SignalTier.PREMIUM,
        SignalTier.STRONG,
    }:
        tier = SignalTier.SILENT_WATCH

    return V15Decision(
''',
    '''    minimum_route_coverage = {
        SignalTier.PREMIUM: 75.0,
        SignalTier.STRONG: 60.0,
        SignalTier.HIGH_RISK_MOMENTUM: 60.0,
        SignalTier.CATALYST_REVIVAL: 60.0,
    }
    if tier in minimum_route_coverage and coverage < minimum_route_coverage[tier]:
        critical.append("EVIDENCE_COVERAGE_BELOW_ROUTE_MINIMUM")
        tier = SignalTier.SILENT_WATCH
    if entry in {EntryStatus.CHASING, EntryStatus.CLOSED, EntryStatus.UNKNOWN} and tier in {
        SignalTier.PREMIUM,
        SignalTier.STRONG,
    }:
        tier = SignalTier.SILENT_WATCH

    return V15Decision(
''',
)

# ---------------------------------------------------------------------------
# Store: authoritative candidate presentation and route/delivery diagnostics.
# ---------------------------------------------------------------------------
STORE = "src/memecoin_bot/database/store.py"
replace_regex(
    STORE,
    r"    def candidates_report\(self, limit: int = 10\) -> list\[sqlite3\.Row\]:\n.*?(?=\n    def )",
    '''    def candidates_report(self, limit: int = 10) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT c.*,t.symbol,t.name,t.token_address,t.chain,"
                "rd.tier AS decision_tier,rd.route_state,rd.confidence AS decision_confidence,"
                "rd.decision_reason FROM candidates c JOIN tokens t ON t.id=c.token_id "
                "LEFT JOIN runner_decisions_v15 rd ON rd.decision_id=("
                "SELECT rd2.decision_id FROM runner_decisions_v15 rd2 "
                "WHERE rd2.candidate_id=c.id ORDER BY rd2.decision_at DESC,rd2.created_at DESC LIMIT 1) "
                "WHERE c.state NOT IN ('REJECTED_UNSAFE','EXPIRED','SIGNALLED') "
                "ORDER BY CASE rd.route_state "
                "WHEN 'PUBLIC_ALERT' THEN 0 WHEN 'OPERATOR_SHADOW_ALERT' THEN 1 ELSE 2 END,"
                "rd.decision_at DESC,c.updated_at DESC LIMIT ?",
                (limit,),
            )
        )

''',
)

replace_once(
    STORE,
    '''        last_alert = self.conn.execute(
            "SELECT last_error FROM outbox WHERE last_error IS NOT NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
        result["last_alert_error"] = last_alert[0] if last_alert else None
''',
    '''        route_counts = {
            str(row["route_state"]): int(row["count"])
            for row in self.conn.execute(
                "SELECT route_state,COUNT(*) AS count FROM runner_decisions_v15 GROUP BY route_state"
            )
        }
        last_decision = self.conn.execute(
            "SELECT decision_at,tier,route_state,decision_reason FROM runner_decisions_v15 "
            "ORDER BY decision_at DESC,created_at DESC LIMIT 1"
        ).fetchone()
        last_qualified = self.conn.execute(
            "SELECT decision_at,tier,route_state,decision_reason FROM runner_decisions_v15 "
            "WHERE route_state IN ('OPERATOR_SHADOW_ALERT','PUBLIC_ALERT') "
            "OR decision_reason='ALERT_ROUTES_DISABLED' "
            "ORDER BY decision_at DESC,created_at DESC LIMIT 1"
        ).fetchone()
        last_signal_outbox = self.conn.execute(
            "SELECT created_at,sent_at,remote_message_id,last_error FROM outbox "
            "WHERE event_type='SIGNAL' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        result["pipeline"] = {
            "route_counts": route_counts,
            "last_decision": dict(last_decision) if last_decision else None,
            "last_qualified": dict(last_qualified) if last_qualified else None,
            "last_signal_outbox": dict(last_signal_outbox) if last_signal_outbox else None,
            "signals_persisted": result["signals"],
            "enabled_alert_destinations": one(
                "SELECT COUNT(*) FROM guild_settings WHERE alerts_enabled=1 "
                "AND alert_channel_id IS NOT NULL"
            ),
            "route_suppressed": one(
                "SELECT COUNT(*) FROM outbox WHERE remote_message_id LIKE 'route-suppressed:%'"
            ),
            "policy_suppressed": one(
                "SELECT COUNT(*) FROM outbox WHERE remote_message_id='policy-suppressed'"
            ),
        }
        last_alert = self.conn.execute(
            "SELECT last_error FROM outbox WHERE last_error IS NOT NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
        result["last_alert_error"] = last_alert[0] if last_alert else None
''',
)

replace_once(
    STORE,
    '''        if event_type in {
            "MILESTONE",
            "RADAR_MILESTONE",
            "RADAR_RISK",
            "FAILED",
            "DETERIORATION",
            "UPGRADE",
        }:
            return True
        if event_type != "SIGNAL":
            return False
        return str(payload.get("v15_signal_tier") or payload.get("signal_tier") or "").upper() in {
            "PREMIUM",
            "STRONG",
            "HIGH_RISK_MOMENTUM",
            "CATALYST_REVIVAL",
        }
''',
    '''        if event_type in {"MILESTONE", "FAILED", "DETERIORATION", "UPGRADE"}:
            return True
        if event_type != "SIGNAL":
            return False
        route_state = str((payload.get("runner_decision") or {}).get("route_state") or "")
        if route_state and route_state not in {"OPERATOR_SHADOW_ALERT", "PUBLIC_ALERT"}:
            return False
        return str(payload.get("v15_signal_tier") or payload.get("signal_tier") or "").upper() in {
            "PREMIUM",
            "STRONG",
            "HIGH_RISK_MOMENTUM",
            "CATALYST_REVIVAL",
        }
''',
)

# ---------------------------------------------------------------------------
# Service: separate qualification from delivery, supervise every long-running
# worker, expose heartbeats, and prevent one provider failure from killing the
# pipeline while Discord remains deceptively online.
# ---------------------------------------------------------------------------
SERVICE = "src/memecoin_bot/service.py"
replace_once(
    SERVICE,
    '_MARKET_UNSET = object()\n',
    '''_MARKET_UNSET = object()
_ROUTABLE_SIGNAL_TIERS = frozenset(
    {"PREMIUM", "STRONG", "HIGH_RISK_MOMENTUM", "CATALYST_REVIVAL"}
)


def authoritative_signal_qualified(
    signal_tier: Any,
    blocking_reasons: list[str],
    hard_rejections: list[str],
) -> bool:
    """Qualification truth is independent of whether a Discord route is enabled."""
    return (
        str(signal_tier) in _ROUTABLE_SIGNAL_TIERS
        and not blocking_reasons
        and not hard_rejections
    )
''',
)

replace_once(
    SERVICE,
    '''        self.log = logging.getLogger("memecoin_bot.service")
        self.stop_event = asyncio.Event()

    def close(self) -> None:
''',
    '''        self.log = logging.getLogger("memecoin_bot.service")
        self.stop_event = asyncio.Event()
        self.worker_state: dict[str, dict[str, Any]] = {}

    def close(self) -> None:
''',
)

replace_once(
    SERVICE,
    '''    async def offer_launch_event(self, event: LaunchEvent) -> None:
''',
    '''    def _mark_worker_cycle(self, name: str, result: Any = None) -> None:
        state = self.worker_state.setdefault(name, {})
        state.update(
            status="RUNNING",
            last_success_at=iso(),
            last_error=None,
            cycles=int(state.get("cycles") or 0) + 1,
            last_result=result,
        )

    async def _supervise_worker(self, name: str, factory: Any) -> None:
        backoff = max(0.05, float(self.settings.launch_source_reconnect_seconds))
        while not self.stop_event.is_set():
            state = self.worker_state.setdefault(name, {})
            state.update(status="RUNNING", last_started_at=iso())
            try:
                await factory()
                if self.stop_event.is_set():
                    break
                raise RuntimeError(f"{name} returned unexpectedly")
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - worker supervisor is a fault boundary
                state.update(
                    status="RESTARTING",
                    last_error=f"{type(error).__name__}: {error}"[:500],
                    last_error_at=iso(),
                    restart_count=int(state.get("restart_count") or 0) + 1,
                )
                log_event(
                    self.log,
                    logging.ERROR,
                    "pipeline_worker_restart",
                    worker=name,
                    restart_count=state["restart_count"],
                    error=state["last_error"],
                )
                try:
                    await asyncio.wait_for(self.stop_event.wait(), timeout=backoff)
                except TimeoutError:
                    pass
                backoff = min(30.0, backoff * 2)
        self.worker_state.setdefault(name, {})["status"] = "STOPPED"

    def runtime_health(self) -> dict[str, Any]:
        now = datetime.now(UTC)
        thresholds = {
            "scanner": max(60.0, self.settings.discovery_interval_seconds * 3),
            "candidate_monitor": max(60.0, self.settings.candidate_monitor_interval_seconds * 3),
            "tracker": max(60.0, self.settings.monitor_interval_seconds * 3),
            "outcome_monitor": max(180.0, self.settings.outcome_monitor_interval_seconds * 3),
        }
        workers: dict[str, Any] = {}
        healthy = True
        starting = False
        for name, threshold in thresholds.items():
            raw = dict(self.worker_state.get(name) or {})
            last_success = raw.get("last_success_at")
            age = None
            if last_success:
                try:
                    age = max(0.0, (now - datetime.fromisoformat(str(last_success))).total_seconds())
                except ValueError:
                    age = None
            stale = age is not None and age > threshold
            if not raw or last_success is None:
                starting = True
            if stale or raw.get("status") == "RESTARTING":
                healthy = False
            raw["heartbeat_age_seconds"] = round(age, 2) if age is not None else None
            raw["stale"] = stale
            workers[name] = raw
        return {
            "status": "HEALTHY" if healthy and not starting else "STARTING" if healthy else "DEGRADED",
            "workers": workers,
            "stop_requested": self.stop_event.is_set(),
        }

    async def offer_launch_event(self, event: LaunchEvent) -> None:
''',
)

replace_once(
    SERVICE,
    '''        waiting = self.safety_gates.readiness(market)
        if safety_unavailable:
            waiting.append("SAFETY_DATA_UNAVAILABLE")
        if momentum.get("score") is None:
            waiting.append(momentum.get("reason", "MOMENTUM_UNAVAILABLE"))
        if score.confidence < self.settings.min_confidence_for_signal:
            waiting.append("INSUFFICIENT_EVIDENCE_COVERAGE")
        entry_state = entry_quality(
            candidate["radar_market_cap_usd"] or market.market_cap_usd, market.market_cap_usd
        )
        if entry_state in {"CHASING", "LATE"}:
            waiting.append("LATE_ENTRY_NOT_QUALIFIED")
''',
    '''        authoritative_waiting = self.safety_gates.readiness(market)
        waiting = list(authoritative_waiting)
        if safety_unavailable:
            authoritative_waiting.append("SAFETY_DATA_UNAVAILABLE")
            waiting.append("SAFETY_DATA_UNAVAILABLE")
        if momentum.get("score") is None:
            # Legacy momentum readiness remains diagnostic evidence, but cannot
            # secretly overrule the sole CONTROL_V15 decision authority.
            waiting.append(momentum.get("reason", "MOMENTUM_UNAVAILABLE"))
        if score.confidence < self.settings.min_confidence_for_signal:
            waiting.append("LEGACY_CONTROL_EVIDENCE_INCOMPLETE")
        entry_state = entry_quality(
            candidate["radar_market_cap_usd"] or market.market_cap_usd, market.market_cap_usd
        )
        if entry_state in {"CHASING", "LATE"}:
            authoritative_waiting.append("LATE_ENTRY_NOT_QUALIFIED")
            waiting.append("LATE_ENTRY_NOT_QUALIFIED")
        authoritative_waiting = sorted(set(authoritative_waiting))
        waiting = sorted(set(waiting))
''',
)

replace_once(
    SERVICE,
    '''        runner_decision = self.runner_decisions.decide(
''',
    '''        operator_route_enabled = self.settings.operator_shadow_alerts_enabled or bool(
            self.store.alert_destinations()
        )
        runner_decision = self.runner_decisions.decide(
''',
)
replace_once(
    SERVICE,
    '            waiting_reasons=sorted(set(waiting)),\n',
    '            waiting_reasons=authoritative_waiting,\n',
)
replace_once(
    SERVICE,
    '            operator_shadow_alerts_enabled=self.settings.operator_shadow_alerts_enabled,\n',
    '            operator_shadow_alerts_enabled=operator_route_enabled,\n',
)
replace_once(
    SERVICE,
    '        signal_grade = runner_decision.routes_alert\n',
    '''        qualified_signal = authoritative_signal_qualified(
            v15_decision.signal_tier,
            authoritative_waiting,
            list(score.hard_rejections),
        )
''',
)
replace_once(SERVICE, '            if signal_grade and not waiting:\n', '            if qualified_signal:\n')
replace_once(
    SERVICE,
    '        if waiting or not signal_grade:\n',
    '        if authoritative_waiting or not qualified_signal:\n',
)
replace_once(
    SERVICE,
    '            reason = waiting[0] if waiting else "SCORE_BELOW_WATCH"\n',
    '''            reason = (
                authoritative_waiting[0]
                if authoritative_waiting
                else f"CONTROL_V15_{v15_decision.signal_tier}"
            )
''',
)

replace_once(
    SERVICE,
    '''            for chain, addresses in by_chain.items():
                for address, snapshot in (await batch_fetch(addresses, chain)).items():
                    prefetched[(chain, address)] = snapshot
''',
    '''            for chain, addresses in by_chain.items():
                try:
                    batch = await batch_fetch(addresses, chain)
                except Exception as error:  # noqa: BLE001 - fall back to individual provider calls
                    log_event(
                        self.log,
                        logging.WARNING,
                        "candidate_batch_prefetch_failed",
                        chain=chain,
                        count=len(addresses),
                        error=f"{type(error).__name__}: {error}",
                    )
                    continue
                for address, snapshot in batch.items():
                    prefetched[(chain, address)] = snapshot
''',
)
replace_once(
    SERVICE,
    '''            results[result] = results.get(result, 0) + 1
        return results

    async def monitor_outcomes_once''',
    '''            results[result] = results.get(result, 0) + 1
            await asyncio.sleep(0)
        return results

    async def monitor_outcomes_once''',
)
replace_once(
    SERVICE,
    '''        except ProviderError as exc:
            log_event(self.log, logging.ERROR, "discovery_failure", error=str(exc))
            return {"DISCOVERY_FAILURE": 1}
''',
    '''        except Exception as exc:  # noqa: BLE001 - discovery worker must remain supervised
            log_event(
                self.log,
                logging.ERROR,
                "discovery_failure",
                error=f"{type(exc).__name__}: {exc}",
            )
            return {"DISCOVERY_FAILURE": 1}
''',
)
replace_once(
    SERVICE,
    '''            results[result] = results.get(result, 0) + 1
        return results

    async def flush_outbox''',
    '''            results[result] = results.get(result, 0) + 1
            await asyncio.sleep(0)
        return results

    async def flush_outbox''',
)

replace_once(
    SERVICE,
    '''                payload = json.loads(row["payload_json"])
                content = format_discord_event(row["event_type"], payload)
                has_guild_settings = bool(
                    self.store.conn.execute("SELECT COUNT(*) FROM guild_settings").fetchone()[0]
                )
''',
    '''                payload = json.loads(row["payload_json"])
                route_state = str(
                    (payload.get("runner_decision") or {}).get("route_state") or ""
                )
                if row["event_type"] == "SIGNAL" and route_state and route_state not in {
                    "OPERATOR_SHADOW_ALERT",
                    "PUBLIC_ALERT",
                }:
                    self.store.mark_outbox_sent(
                        int(row["id"]), f"route-suppressed:{route_state}", claim_token
                    )
                    log_event(
                        self.log,
                        logging.INFO,
                        "signal_delivery_suppressed",
                        outbox_id=row["id"],
                        route_state=route_state,
                        reason=(payload.get("runner_decision") or {}).get("decision_reason"),
                    )
                    sent += 1
                    continue
                content = format_discord_event(row["event_type"], payload)
                has_guild_settings = bool(
                    self.store.conn.execute(
                        "SELECT COUNT(*) FROM guild_settings WHERE alerts_enabled=1 "
                        "AND alert_channel_id IS NOT NULL"
                    ).fetchone()[0]
                )
''',
)

replace_regex(
    SERVICE,
    r"    async def run\(self\) -> None:\n.*?(?=\n    def stop\(self\) -> None:)",
    '''    async def run(self) -> None:
        recovered_realtime = self.realtime_fabric.recover_stale_claims()
        log_event(
            self.log,
            logging.INFO,
            "restart_recovery",
            active_signals=len(self.store.active_signals()),
            pending_outbox=len(self.store.pending_outbox()),
            recovered_realtime_claims=recovered_realtime,
        )

        async def scanner() -> None:
            while not self.stop_event.is_set():
                result = await self.scan_once()
                delivered = await self.flush_outbox()
                self._mark_worker_cycle("scanner", {"scan": result, "delivered": delivered})
                try:
                    await asyncio.wait_for(
                        self.stop_event.wait(), self.settings.discovery_interval_seconds
                    )
                except TimeoutError:
                    pass

        async def tracker() -> None:
            while not self.stop_event.is_set():
                result = await self.tracker.monitor_once()
                delivered = await self.flush_outbox()
                self._mark_worker_cycle("tracker", {"tracked": result, "delivered": delivered})
                try:
                    await asyncio.wait_for(
                        self.stop_event.wait(), self.settings.monitor_interval_seconds
                    )
                except TimeoutError:
                    pass

        async def candidate_monitor() -> None:
            while not self.stop_event.is_set():
                result = await self.monitor_candidates_once()
                delivered = await self.flush_outbox()
                self._mark_worker_cycle(
                    "candidate_monitor", {"candidates": result, "delivered": delivered}
                )
                try:
                    await asyncio.wait_for(
                        self.stop_event.wait(), self.settings.candidate_monitor_interval_seconds
                    )
                except TimeoutError:
                    pass

        async def outcome_monitor() -> None:
            while not self.stop_event.is_set():
                result = await self.monitor_outcomes_once()
                self._mark_worker_cycle("outcome_monitor", {"monitored": result})
                try:
                    await asyncio.wait_for(
                        self.stop_event.wait(), self.settings.outcome_monitor_interval_seconds
                    )
                except TimeoutError:
                    pass

        async def launch_worker() -> None:
            while not self.stop_event.is_set():
                try:
                    launch = await asyncio.wait_for(self.launch_queue.queue.get(), timeout=0.5)
                except TimeoutError:
                    continue
                try:
                    result = await self.handle_launch_event(launch)
                    self._mark_worker_cycle("launch_worker", {"result": result})
                finally:
                    self.launch_queue.task_done(launch)

        async def realtime_worker() -> None:
            lanes = TokenLaneExecutor(
                self.settings.realtime_token_lanes,
                queue_size=max(
                    self.settings.realtime_processing_batch * 2,
                    self.settings.realtime_token_lanes,
                ),
            )

            def on_error(realtime_event: CanonicalEvent, error: Exception) -> None:
                self.log.error(
                    "canonical event processing failed",
                    extra={"fields": {"event_id": realtime_event.event_id}},
                    exc_info=(type(error), error, error.__traceback__),
                )

            self._mark_worker_cycle("realtime_worker", {"state": "STARTED"})
            await lanes.run(
                claim=self.realtime_fabric.claim_pending,
                handle=self.handle_realtime_event,
                fail=self.realtime_fabric.fail,
                wake=self.realtime_wake,
                stop=self.stop_event,
                batch_size=self.settings.realtime_processing_batch,
                on_error=on_error,
            )

        workers: list[tuple[str, Any]] = [
            ("scanner", scanner),
            ("candidate_monitor", candidate_monitor),
            ("outcome_monitor", outcome_monitor),
            ("tracker", tracker),
        ]
        if self.launch_sources:
            workers.append(("launch_worker", launch_worker))
            for index, source in enumerate(self.launch_sources):
                workers.append(
                    (
                        f"launch_source:{index}:{type(source).__name__}",
                        lambda source=source: source.run(self.offer_launch_event, self.stop_event),
                    )
                )
        if self.settings.realtime_fabric_enabled:
            workers.append(("realtime_worker", realtime_worker))
        for index, source in enumerate(self.realtime_sources):
            workers.append(
                (
                    f"realtime_source:{index}:{type(source).__name__}",
                    lambda source=source: source.run_events(
                        self.offer_realtime_event, self.stop_event
                    ),
                )
            )
        await asyncio.gather(
            *(self._supervise_worker(name, factory) for name, factory in workers)
        )
''',
)

# ---------------------------------------------------------------------------
# Product presentation: expose the reason the pipeline is silent rather than
# letting an online Discord bot masquerade as a healthy intelligence service.
# ---------------------------------------------------------------------------
POLICY = "src/memecoin_bot/discord/product_policy.py"
replace_once(
    POLICY,
    '''    cards.card = branded_card
    command_center.card = branded_card

    original_format = formatting.format_discord_event
''',
    '''    cards.card = branded_card
    command_center.card = branded_card

    original_status_card = cards.status_card

    @functools.wraps(original_status_card)
    def reliability_status_card(stats: dict[str, Any]) -> dict[str, Any]:
        payload = original_status_card(stats)
        runtime = stats.get("runtime") or {}
        pipeline = stats.get("pipeline") or {}
        workers = runtime.get("workers") or {}
        worker_lines = []
        for name in ("scanner", "candidate_monitor", "tracker", "outcome_monitor"):
            state = workers.get(name) or {}
            worker_lines.append(
                f"{name.replace('_', ' ').title()}: "
                f"{state.get('status') or 'STARTING'} • "
                f"heartbeat {state.get('heartbeat_age_seconds') if state.get('heartbeat_age_seconds') is not None else 'pending'}s • "
                f"restarts {state.get('restart_count') or 0}"
            )
        last_decision = pipeline.get("last_decision") or {}
        last_qualified = pipeline.get("last_qualified") or {}
        last_outbox = pipeline.get("last_signal_outbox") or {}
        payload["embed"]["fields"].extend(
            [
                {
                    "name": "PIPELINE RUNTIME",
                    "value": (
                        f"Overall: **{runtime.get('status') or 'STARTING'}**\n"
                        + "\n".join(worker_lines)
                    )[:1024],
                    "inline": False,
                },
                {
                    "name": "CALL / DELIVERY TRUTH",
                    "value": (
                        f"Last decision: **{last_decision.get('tier') or 'NONE'}** • "
                        f"{last_decision.get('route_state') or 'NONE'} • "
                        f"{last_decision.get('decision_reason') or 'NO DECISION'}\n"
                        f"Last qualified: **{last_qualified.get('tier') or 'NONE'}** • "
                        f"{last_qualified.get('decision_at') or 'NONE'}\n"
                        f"Last signal outbox: created {last_outbox.get('created_at') or 'NONE'} • "
                        f"sent {last_outbox.get('sent_at') or 'NO'} • "
                        f"remote {last_outbox.get('remote_message_id') or 'NONE'}\n"
                        f"Enabled destinations: {pipeline.get('enabled_alert_destinations', 0)} • "
                        f"route-suppressed: {pipeline.get('route_suppressed', 0)} • "
                        f"policy-suppressed: {pipeline.get('policy_suppressed', 0)}"
                    )[:1024],
                    "inline": False,
                },
            ]
        )
        return apply_product_presentation(payload)

    cards.status_card = reliability_status_card
    bot_runtime.status_card = reliability_status_card
    command_center.status_card = reliability_status_card

    original_format = formatting.format_discord_event
''',
)

# ---------------------------------------------------------------------------
# New regression/E2E suite.
# ---------------------------------------------------------------------------
TEST = r'''from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from memecoin_bot.discord import bot_runtime
from memecoin_bot.models import DiscoveryEvent, MarketSnapshot, SafetyAssessment, iso
from memecoin_bot.service import IntelligenceService
from memecoin_bot.v15_engine import (
    EntryStatus,
    SignalTier,
    Stage,
    V15Decision,
    evaluate_v15,
)
from tests.helpers import settings, store, temp_db_path
from tests.test_candidate_lifecycle import EmptyDiscovery
from tests.test_discord_command_center import (
    FakeInteraction,
    FakeService,
    FakeStore,
    capture_runtime,
    primary_payload,
)


class SafeSafety:
    name = "safe-safety"

    async def safety(self, chain: str, address: str) -> SafetyAssessment:
        return SafetyAssessment(
            checked_at=iso(),
            source=self.name,
            chain=chain,
            top10_percent=20,
            holder_count=100,
        )


class FixedMarket:
    name = "fixed-market"

    def __init__(self, address: str, *, fail_batch: bool = False):
        self.address = address
        self.fail_batch = fail_batch
        self.snapshot = MarketSnapshot(
            token_address=address,
            captured_at=iso(),
            source=self.name,
            chain="solana",
            pair_address="pair-1",
            symbol="PIPE",
            name="Pipeline",
            pair_created_at=(datetime.now(UTC) - timedelta(minutes=2)).isoformat(),
            market_cap_usd=25_000,
            price_usd=0.000025,
            liquidity_usd=30_000,
            volume_5m_usd=20_000,
            buys_5m=60,
            sells_5m=10,
            price_change_5m=25,
        )

    async def market_snapshot(self, address: str, chain: str = "solana") -> MarketSnapshot:
        self.snapshot.captured_at = iso()
        return self.snapshot

    async def market_snapshots(self, addresses: list[str], chain: str = "solana"):
        if self.fail_batch:
            raise RuntimeError("synthetic batch outage")
        return {address: await self.market_snapshot(address, chain) for address in addresses}


class RecordingNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[int | None, dict]] = []

    async def send_to(self, channel_id: int, content: dict) -> str:
        self.sent.append((channel_id, content))
        return f"message-{len(self.sent)}"

    async def send(self, content: dict) -> str:
        self.sent.append((None, content))
        return f"message-{len(self.sent)}"


def strong_decision(*_args, **_kwargs) -> V15Decision:
    return V15Decision(
        stage=Stage.NEW,
        runner_score=82,
        runner_grade="HIGH",
        failure_score=10,
        failure_grade="LOW",
        survival_grade="HIGH",
        setup_conviction=82,
        evidence_coverage=86,
        entry_status=EntryStatus.OPEN,
        signal_tier=SignalTier.STRONG,
        why_now=["tradeable liquidity", "momentum acceleration"],
        feature_vector={},
    )


def build_service(path, *, route_enabled: bool, guild_destination: bool, fail_batch: bool = False):
    config = settings(path)
    config.operator_shadow_alerts_enabled = route_enabled
    config.public_alerts_enabled = False
    config.min_snapshots_for_momentum = 1
    config.launch_source_reconnect_seconds = 0.01
    address = "Pipeline111111111111111111111111111111111"
    database = store(path)
    if guild_destination:
        database.set_guild_settings(
            101,
            202,
            True,
            "QUALIFIED_ONLY",
            303,
            False,
            ["solana"],
        )
    notifier = RecordingNotifier()
    service = IntelligenceService(
        config,
        database,
        EmptyDiscovery(),
        FixedMarket(address, fail_batch=fail_batch),
        SafeSafety(),
        notifier,
    )
    return service, database, notifier, address


def test_all_registered_commands_defer_before_work() -> None:
    assert bot_runtime.DEFERRED_COMMANDS == bot_runtime.EXPECTED_COMMAND_NAMES


def test_low_coverage_high_partial_score_is_silent_watch() -> None:
    from memecoin_bot.v15_engine import STAGE_FEATURES

    required = STAGE_FEATURES[Stage.NEW]
    features = {
        name: 95 if index < 4 else None
        for index, name in enumerate(required)
    }
    features.update(call_market_cap=10_000, current_market_cap=11_000, age_minutes=5)
    result = evaluate_v15(Stage.NEW, features)
    assert result.evidence_coverage < 60
    assert result.signal_tier == SignalTier.SILENT_WATCH
    assert "EVIDENCE_COVERAGE_BELOW_ROUTE_MINIMUM" in result.critical_unknowns


@pytest.mark.asyncio
async def test_test_alert_card_survives_optional_audit_failure() -> None:
    with patch.object(FakeStore, "record_test_alert", side_effect=RuntimeError("audit locked")):
        tree, _client, _store = await capture_runtime()
        interaction = FakeInteraction(admin=True)
        await tree.get_command("test-alert").callback(interaction)
        payload = primary_payload(interaction)
        assert payload["embed"].title == "GAMBIT JR • TEST ALERT"
        assert "couldn't complete" not in str(payload).lower()


@pytest.mark.asyncio
async def test_slow_command_is_acknowledged_then_fails_safely(monkeypatch) -> None:
    async def slow_scan(self, *_args, **_kwargs):
        await asyncio.sleep(0.1)
        return {}

    monkeypatch.setattr(bot_runtime, "COMMAND_TIMEOUT_SECONDS", 0.01)
    with patch.object(FakeService, "manual_scan", slow_scan):
        tree, _client, _store = await capture_runtime()
        interaction = FakeInteraction()
        await tree.get_command("scan").callback(interaction, "So111", "solana")
        assert interaction.response.deferred_at is not None
        payload = primary_payload(interaction)
        assert "safe time limit" in payload["content"]


@pytest.mark.asyncio
async def test_candidate_card_never_presents_partial_100_as_final_score() -> None:
    with patch.object(
        FakeStore,
        "candidates_report",
        return_value=[
            {
                "name": "Sparse Candidate",
                "chain": "solana",
                "state": "PENDING_EVIDENCE",
                "normalized_score": 100.0,
                "reason": "INSUFFICIENT_EVIDENCE",
                "route_state": "HOLD",
            }
        ],
    ):
        tree, _client, _store = await capture_runtime()
        interaction = FakeInteraction()
        await tree.get_command("candidates").callback(interaction)
        description = primary_payload(interaction)["embed"].description
        assert "score 100" not in description.lower()
        assert "developing setup" in description.lower()


@pytest.mark.asyncio
async def test_worker_supervisor_restarts_and_recovers() -> None:
    with temp_db_path() as path:
        service, database, _notifier, _address = build_service(
            path, route_enabled=False, guild_destination=False
        )
        attempts = 0

        async def flaky_worker() -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("synthetic worker crash")
            service._mark_worker_cycle("synthetic", {"recovered": True})
            service.stop()

        await asyncio.wait_for(
            service._supervise_worker("synthetic", flaky_worker), timeout=1
        )
        assert attempts == 2
        assert service.worker_state["synthetic"]["restart_count"] == 1
        assert service.worker_state["synthetic"]["cycles"] == 1
        database.close()


@pytest.mark.asyncio
async def test_batch_provider_failure_falls_back_without_killing_candidate_monitor() -> None:
    with temp_db_path() as path:
        service, database, _notifier, address = build_service(
            path, route_enabled=False, guild_destination=False, fail_batch=True
        )
        token_id, _ = database.upsert_discovery(
            DiscoveryEvent(token_address=address, source="test")
        )
        database.ensure_candidate(token_id, iso(), service.settings.scoring_version)
        with patch("memecoin_bot.service.evaluate_v15", side_effect=strong_decision):
            result = await service.monitor_candidates_once()
        assert sum(result.values()) == 1
        assert database.conn.execute("SELECT COUNT(*) FROM runner_decisions_v15").fetchone()[0] == 1
        database.close()


@pytest.mark.asyncio
async def test_qualified_call_persists_when_delivery_route_is_disabled() -> None:
    with temp_db_path() as path:
        service, database, notifier, address = build_service(
            path, route_enabled=False, guild_destination=False
        )
        with patch("memecoin_bot.service.evaluate_v15", side_effect=strong_decision):
            await service.evaluate(DiscoveryEvent(token_address=address, source="test"))
        assert database.conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 1
        decision = database.conn.execute(
            "SELECT tier,route_state,decision_reason FROM runner_decisions_v15 "
            "ORDER BY decision_at DESC LIMIT 1"
        ).fetchone()
        assert decision["tier"] == "STRONG"
        assert decision["route_state"] == "HOLD"
        assert decision["decision_reason"] == "ALERT_ROUTES_DISABLED"
        assert await service.flush_outbox() == 1
        assert notifier.sent == []
        outbox = database.conn.execute(
            "SELECT sent_at,remote_message_id,last_error FROM outbox WHERE event_type='SIGNAL'"
        ).fetchone()
        assert outbox["sent_at"] is not None
        assert outbox["remote_message_id"] == "route-suppressed:HOLD"
        assert outbox["last_error"] is None
        database.close()


@pytest.mark.asyncio
async def test_full_discovery_to_discord_pipeline_delivers_once() -> None:
    with temp_db_path() as path:
        # Explicit /setup-style guild configuration is sufficient consent for an
        # operator-shadow call even when the environment flag is false.
        service, database, notifier, address = build_service(
            path, route_enabled=False, guild_destination=True
        )
        with patch("memecoin_bot.service.evaluate_v15", side_effect=strong_decision):
            result = await service.evaluate(
                DiscoveryEvent(token_address=address, source="test")
            )
        assert result == "STRONG"
        assert database.conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 1
        assert database.conn.execute(
            "SELECT route_state FROM runner_decisions_v15 ORDER BY decision_at DESC LIMIT 1"
        ).fetchone()[0] == "OPERATOR_SHADOW_ALERT"
        assert await service.flush_outbox() == 1
        assert len(notifier.sent) == 1
        assert notifier.sent[0][0] == 202
        assert await service.flush_outbox() == 0
        assert len(notifier.sent) == 1
        delivery = database.conn.execute(
            "SELECT status,attempts,remote_message_id FROM alert_deliveries_v131"
        ).fetchone()
        assert dict(delivery) == {
            "status": "SENT",
            "attempts": 1,
            "remote_message_id": "message-1",
        }
        diagnostics = database.status_stats(service.started_at)["pipeline"]
        assert diagnostics["enabled_alert_destinations"] == 1
        assert diagnostics["last_qualified"]["tier"] == "STRONG"
        database.close()
'''
write("tests/test_pipeline_reliability_v2.py", TEST)

print("pipeline reliability v2 patch applied")
