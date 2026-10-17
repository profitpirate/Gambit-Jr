"""Dedicated frozen-strategy runtime; no legacy strategy or guardian imports.

All order intents are durable before execution. Unknown outcomes block new buys.
An exit decision is acknowledged only by a confirmed transaction token delta.
"""

from __future__ import annotations

import asyncio
import copy
import json
import math
import re
import sqlite3
import time
import uuid
from dataclasses import asdict, replace
from pathlib import Path
from urllib.parse import urlsplit

from .v12_prearmed_diagnostics import DiagnosticError, Diagnostics, failure, quote_facts
from .v12_prearmed_evidence import FROZEN_MODEL_SHA256
from .v12_prearmed_io import metadata
from .v12_prearmed_metadata import MetadataRequests
from .v12_prearmed_library import LibraryPrearmedPolicy
from .v12_prearmed_policy import EntryFacts, ExitState, FrozenPrearmedPolicy
from .v12_safety import CircuitBreaker, SafetyStore
from .v12_prearmed_reporting import TradeReports
from .v12_prearmed_timing import BUILDER_TIMINGS


def social_status(document):
    extensions = document.get("extensions") or {}
    if not isinstance(extensions, dict):
        raise DiagnosticError("SOCIAL_EXTENSIONS_INVALID")
    value = document.get("twitter") or extensions.get("twitter", "")
    url = urlsplit(str(value))
    if url.scheme != "https" or url.hostname not in {
        "x.com",
        "www.x.com",
        "twitter.com",
        "www.twitter.com",
    }:
        raise DiagnosticError("SOCIAL_STATUS_URL_REQUIRED")
    match = re.fullmatch(r"/([A-Za-z0-9_]{1,15})/status/([0-9]{10,22})/?", url.path)
    if not match:
        raise DiagnosticError("SOCIAL_STATUS_PATH_INVALID")
    snowflake = int(match[2])
    return match[1].lower(), ((snowflake >> 22) + 1288834974657) * 1_000_000


def reserve_price(event):
    sol, tokens = event.get("virtual_sol_reserves"), event.get("virtual_token_reserves")
    if type(sol) is not int or type(tokens) is not int or sol <= 0 or tokens <= 0:
        raise ValueError("invalid reserves")
    return sol / tokens / 1000


class State:
    def __init__(self, path, *, wallet, mode):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, isolation_level=None)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS frozen_state (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL)"
        )
        self.conn.execute("CREATE TABLE IF NOT EXISTS frozen_seen (mint TEXT PRIMARY KEY)")
        row = self.conn.execute("SELECT data FROM frozen_state WHERE id=1").fetchone()
        self.data = (
            json.loads(row[0])
            if row
            else {
                "wallet": wallet,
                "mode": mode,
                "model": FROZEN_MODEL_SHA256,
                "positions": {},
                "pending": {},
                "closed": 0,
                "realized_lamports": 0,
            }
        )
        if any(
            self.data[k] != v
            for k, v in {"wallet": wallet, "mode": mode, "model": FROZEN_MODEL_SHA256}.items()
        ):
            raise RuntimeError("STATE_WALLET_MODE_OR_MODEL_MISMATCH")
        self.save()
        self.reports = TradeReports(self.conn, self.data)

    def save(self):
        self.conn.execute(
            "INSERT OR REPLACE INTO frozen_state VALUES(1,?)",
            (json.dumps(self.data, allow_nan=False, separators=(",", ":")),),
        )

    def reserve_mint(self, mint):
        return (
            self.conn.execute("INSERT OR IGNORE INTO frozen_seen VALUES(?)", (mint,)).rowcount == 1
        )


class Runtime:
    def __init__(
        self,
        *,
        model,
        state,
        rpc,
        builder,
        execution,
        wallet,
        metadata_session,
        emit,
        kill_path,
        clock=time.time_ns,
        position_percent=10,
        maximum_positions=2,
        creator_library=None,
    ):
        if type(position_percent) is not int or not 1 <= position_percent <= 10:
            raise ValueError("INVALID_POSITION_PERCENT")
        if type(maximum_positions) is not int or not 1 <= maximum_positions <= 2:
            raise ValueError("INVALID_POSITION_LIMIT")
        self.position_percent, self.maximum_positions = position_percent, maximum_positions
        self.model, self.policy, self.state = model, FrozenPrearmedPolicy(model), state
        self.creator_library = creator_library
        if creator_library is not None:
            self.policy = LibraryPrearmedPolicy(model, creator_library)
        self.rpc, self.builder, self.execution, self.wallet = rpc, builder, execution, wallet
        self.metadata_session, self.emit = metadata_session, emit
        self.metadata_requests = MetadataRequests()
        self.diagnostics = Diagnostics(emit, clock, uuid.uuid4().hex)
        self.started_ns = clock()
        self.failed_background_tasks = set()
        self.kill_path, self.clock = Path(kill_path), clock
        self.safety = SafetyStore(state.conn)
        self.breaker = CircuitBreaker(self.safety)
        snapshot = self.safety.snapshot()
        self.last_safety = (snapshot.mode.value, snapshot.reason)
        self.connected, self.last_feed_ns, self.balance = False, 0, 0
        self.balance_ns, self.stopping = 0, False
        self.balance_generation = 0
        self.balance_refresh_requested = asyncio.Event()
        self.balance_refresh_started_ns = 0
        self.balance_refresh_duration_ms = 0
        self.balance_refresh_inflight = False
        self.busy_mints, self.tasks, self.signatures, self.inflight_orders = set(), set(), {}, set()
        self.sequence, self.feed_events, self.candidates = 0, 0, 0
        self.failures = self.state.data.setdefault("failed_order_ns", [])
        self.last_reconcile_ns = 0
        if self.execution is not None:
            self.execution.allow_buy = self.entry_conditions
            self.execution.trace = self.diagnostics.record

    def spawn(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.add(task)

        def done(t):
            self.tasks.discard(t)
            if not t.cancelled() and t.exception() is not None:
                self.breaker.exit_only("runtime_task_failed")
                self.diagnostics.record(
                    "RUNTIME_TASK_FAILED",
                    stage="runtime_task",
                    category="fault",
                    **failure("runtime_task", t.exception()),
                )

        task.add_done_callback(done)
        return task

    def feed_health(self, connected):
        self.connected = connected

    def entry_conditions(self):
        now = self.clock()
        return (
            not self.stopping
            and self.connected
            and 0 <= now - self.last_feed_ns < 5_000_000_000
            and 0 <= now - self.balance_ns < 5_000_000_000
            and self.balance > 30_000_000
            and not self.kill_path.exists()
            and self.safety.snapshot().entries_allowed
        )

    def entries_allowed(self):
        return self.entry_conditions() and not self.state.data["pending"]

    async def refresh_balance(self):
        previous = self.balance_ns
        generation = self.balance_generation
        self.balance_refresh_started_ns = self.clock()
        self.balance_refresh_inflight = True
        try:
            balance = await self.rpc.balance_lamports(self.wallet)
        finally:
            self.balance_refresh_duration_ms = max(0, (self.clock() - self.balance_refresh_started_ns) / 1e6)
            self.balance_refresh_inflight = False
        # A read started before a confirmed/failed transaction cannot make the
        # post-transaction wallet snapshot fresh, even if its reply arrives
        # after the receipt. The single refresh loop will issue a fresh read.
        if generation != self.balance_generation:
            self.balance_refresh_requested.set()
            return
        self.balance = balance
        self.balance_ns = self.clock()
        self.diagnostics.timings.head(getattr(self.rpc, 'last_balance_slot', None), self.balance_ns)
        self.diagnostics.timings.observe('balance_refresh_ms', self.balance_refresh_duration_ms, self.balance_ns)
        gap_ms = (self.balance_ns - previous) / 1e6 if previous else None
        if self.balance_refresh_duration_ms >= 1500 or (gap_ms is not None and gap_ms >= 5000):
            self.diagnostics.record("BALANCE_REFRESH_DELAYED", stage="balance",
                                    reason="BALANCE_REFRESH_TIMING", category="progress",
                                    elapsed_ms=self.balance_refresh_duration_ms,
                                    balance_refresh_gap_ms=gap_ms)
        # Closed cash-equity checks avoid attributing the whole entry cost as a loss.
        # Open positions still have independent stop/hold exits and cannot add risk.
        if not self.state.data["positions"] and not self.state.data["pending"]:
            self.breaker.evaluate_equity(self.balance / 1e9)
        self.breaker.evaluate_operational()

    def invalidate_balance(self):
        self.balance_generation += 1
        self.balance_ns = 0
        self.balance_refresh_requested.set()

    async def wait_for_balance_refresh(self, timeout=1.0):
        try:
            await asyncio.wait_for(self.balance_refresh_requested.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return False
        self.balance_refresh_requested.clear()
        return True

    async def on_batch(self, signature, events, observed_ns):
        if signature in self.signatures:
            self.diagnostics.counts["DUPLICATE_BATCH"] += 1
            return
        self.signatures[signature] = observed_ns
        if len(self.signatures) > 10_000:
            self.signatures.pop(next(iter(self.signatures)))
        self.last_feed_ns = observed_ns
        self.feed_events += len(events)
        if events:
            self.diagnostics.timings.feed(events[0], observed_ns, self.clock())
        for event in events:
            kind, mint = event.get("anchor_event"), event.get("mint")
            self.sequence += 1
            self.buffer_pending_market(signature, event, observed_ns)
            position = self.state.data["positions"].get(mint)
            own_entry = (position and signature == position.get("entry_signature")
                         and kind == "TradeEvent" and event.get("user") == self.wallet
                         and event.get("is_buy") is True)
            if position and not own_entry and kind in {
                "TradeEvent",
                "CompleteEvent",
                "CompletePumpAmmMigrationEvent",
            }:
                try:
                    price = (
                        reserve_price(event) if kind == "TradeEvent" else position["latest_price"]
                    )
                    self.consider_exit(
                        mint, observed_ns, price=price, complete=kind != "TradeEvent"
                    )
                except ValueError:
                    self.breaker.exit_only("invalid_position_market_data")
                    self.diagnostics.record(
                        "POSITION_DATA_REJECTED",
                        stage="exit",
                        category="fault",
                        reason="INVALID_POSITION_MARKET_DATA",
                        mint=mint,
                    )
            if kind != "CreateEvent":
                continue
            creator = event.get("creator")
            if event is not events[0]:
                stamp = event.get('timestamp')
                if type(stamp) is int:
                    self.diagnostics.timings.observe('create_age_approx_ms',
                        (observed_ns-stamp*1e9)/1e6, self.clock())

            def filtered(
                reason,
                category="policy",
                *,
                candidate_mint=mint,
                candidate_creator=creator,
                **details,
            ):
                self.diagnostics.record(
                    "CANDIDATE_FILTERED",
                    stage="eligibility",
                    reason=reason,
                    category=category,
                    mint=candidate_mint,
                    signature=signature,
                    creator=candidate_creator,
                    feed_received_ns=observed_ns,
                    feed_slot=event.get('_feed_slot'),
                    chain_timestamp_seconds=event.get('timestamp'),
                    **self.library_details(candidate_creator),
                    **details,
                )

            self.diagnostics.counts["CREATE_SEEN"] += 1
            if not self.entries_allowed():
                self.diagnostics.counts["FILTER_ENTRY_BLOCKED"] += 1
                filtered("ENTRIES_BLOCKED", "blocked", blockers=self.entry_blockers())
                continue
            known_creator = self.policy.creator_known(creator)
            if not known_creator or mint in self.busy_mints:
                self.diagnostics.counts["FILTER_UNKNOWN_CREATOR_OR_BUSY"] += 1
                filtered(
                    (
                        "CREATOR_NOT_RECOGNISED"
                        if self.creator_library
                        else "CREATOR_NOT_IN_FROZEN_MODEL"
                    )
                    if not known_creator
                    else "MINT_ALREADY_BUSY"
                )
                continue
            self.diagnostics.counts["KNOWN_CREATOR_SEEN"] += 1
            if event.get("is_mayhem_mode") is not False or event.get("quote_mint") not in (
                None,
                "11111111111111111111111111111111",
                "So11111111111111111111111111111111111111112",
            ):
                self.diagnostics.counts["FILTER_UNSUPPORTED_LAUNCH"] += 1
                filtered("UNSUPPORTED_LAUNCH_TYPE")
                continue
            seed = sum(
                e.get("sol_amount", 0)
                for e in events
                if e.get("anchor_event") == "TradeEvent"
                and e.get("mint") == mint
                and e.get("user") == creator
                and e.get("is_buy") is True
            )
            if seed < 2_000_000_000:
                self.diagnostics.counts["FILTER_SEED_BELOW_FLOOR"] += 1
                filtered(
                    "CREATOR_SEED_BELOW_MINIMUM",
                    creator_seed_lamports=seed,
                    minimum_seed_lamports=2_000_000_000,
                )
                continue
            if len(self.state.data["positions"]) + len(self.busy_mints) >= self.maximum_positions:
                self.diagnostics.counts["FILTER_POSITION_CAPACITY"] += 1
                filtered("POSITION_CAPACITY_REACHED")
                continue
            if not self.state.reserve_mint(mint):
                self.diagnostics.counts["FILTER_ALREADY_SEEN"] += 1
                filtered("MINT_PREVIOUSLY_EVALUATED")
                continue
            self.busy_mints.add(mint)
            self.spawn(self.enter(event, seed, observed_ns, signature=signature))

    def buffer_pending_market(self, signature, event, observed_ns):
        """Persist observations, but never treat a feed notification as a token receipt."""
        kind, mint = event.get("anchor_event"), event.get("mint")
        if kind not in {"TradeEvent", "CompleteEvent", "CompletePumpAmmMigrationEvent"}:
            return
        for order in self.state.data["pending"].values():
            if order["payload"].get("side") != "BUY" or order["payload"].get("mint") != mint:
                continue
            market = order.setdefault("market", [])
            previously_incomplete = order.get("market_incomplete", False)
            if len(market) >= 512:
                order["market_incomplete"] = True
            else:
                try:
                    price = reserve_price(event) if kind == "TradeEvent" else None
                except ValueError:
                    order["market_incomplete"] = True
                else:
                    market.append(dict(signature=signature, observed_ns=observed_ns,
                                       sequence=self.sequence, price=price,
                                       complete=kind != "TradeEvent",
                                       wallet_buy=event.get("user") == self.wallet
                                       and event.get("is_buy") is True))
            self.state.save()
            if order.get("market_incomplete") and not previously_incomplete:
                self.diagnostics.record("PENDING_MARKET_INCOMPLETE", stage="exit",
                                        category="fault", reason="PROTECTIVE_EXIT_AFTER_BUY_RECEIPT",
                                        mint=mint, pending_market_events=len(market))

    def restore_buy_market(self, order, result, position):
        """Use the earliest verified fill observation; replay only its later market events.

        A receipt proves the matching feed signature. Without that match a fresh
        receipt is the first known fill observation, not an exact chain timestamp.
        Recovery never assigns an old unknown buy a new holding window.
        """
        submitted = order["payload"]["submitted_ns"]
        now = self.clock()
        receipt_ns = result.get("confirmed_ns")
        valid_receipt = type(receipt_ns) is int and submitted <= receipt_ns <= now
        market = order.get("market", [])
        matches = [row for row in market if row["wallet_buy"]
                   and result.get("signature") and row["signature"] == result["signature"]
                   and submitted <= row["observed_ns"] <= now]
        anchor = min(matches, key=lambda row: (row["observed_ns"], row["sequence"])) if matches else None
        uncertain = False
        if (anchor and anchor["observed_ns"] - submitted <= self.policy.hold_ns
                and (not valid_receipt or anchor["observed_ns"] <= receipt_ns)):
            fill, source = anchor["observed_ns"], "CONFIRMED_FEED_MATCH"
        elif (valid_receipt and receipt_ns - submitted <= self.policy.hold_ns
              and (order.get("runtime_id") == self.diagnostics.run_id
                   or receipt_ns <= self.started_ns)):
            fill, source = receipt_ns, "VERIFIED_RECEIPT_OBSERVED"
            anchor = None
        else:
            fill, source = submitted, "RECOVERY_CONSERVATIVE_BOUND"
            uncertain = True
            anchor = None
        position["exit"]["fill_ns"] = fill
        position["fill_clock_source"] = source
        position["receipt_observed_ns"] = receipt_ns if valid_receipt else None
        position["replayed_market_events"] = 0
        position["pending_market_events"] = len(market)
        if uncertain or order.get("market_incomplete"):
            position["forced_exit"] = True
            position["recovery_exit_reason"] = (
                "PENDING_MARKET_INCOMPLETE" if order.get("market_incomplete") else "FILL_CLOCK_UNCERTAIN")
        if anchor:
            for row in sorted(market, key=lambda row: (row["observed_ns"], row["sequence"])):
                if (row["observed_ns"], row["sequence"]) <= (fill, anchor["sequence"]):
                    continue
                if row["observed_ns"] > now:
                    position["forced_exit"] = True
                    position["recovery_exit_reason"] = "PENDING_MARKET_CLOCK_INVALID"
                    continue
                self.update_exit_state(position, row["observed_ns"],
                                       price=row["price"] if row["price"] is not None else position["latest_price"],
                                       complete=row["complete"], sequence=row["sequence"])
                position["replayed_market_events"] += 1

    def update_exit_state(self, position, observed_ns, *, price=None, complete=False,
                          timer_only=False, sequence=None):
        current = ExitState(**position["exit"])
        update = self.policy.exits(current, observed_ns=observed_ns, price=price,
                                   complete=complete, timer_only=timer_only,
                                   sequence=self.sequence if sequence is None else sequence)
        # Keep triggered decisions durable while receipts are pending. Only actual
        # token deltas acknowledge a take/close; a full exit supersedes a take.
        for action in update.actions:
            if action.reason != "FIRST_TAKE" or not position.get("exit_latched_reason"):
                position["exit_latched_reason"] = action.reason
        position["exit"] = asdict(replace(update.state,
                                         first_partial_done=current.first_partial_done, closed=False))
        if price is not None and update.state is not current:
            position["latest_price"] = price
        return update

    async def enter(self, event, seed, observed_ns, *, signature=None):
        mint = event["mint"]
        stage = "metadata"
        started_ns = self.clock()
        self.diagnostics.timings.observe('candidate_queue_ms', (started_ns-observed_ns)/1e6, started_ns)
        metadata_context = {}
        quote_task = None
        prefetch_request = None

        async def prefetch_quote(request):
            # Read-only work, limited by the existing two-candidate capacity.
            # Capture failures: invalid metadata must remain the primary reason
            # for rejection, and no speculative request may place an order.
            try:
                result = await self.builder.request({**request, "action": "QUOTE"})
                return self.clock(), result, None
            except (KeyError, TypeError, ValueError, RuntimeError, OSError, TimeoutError) as error:
                return self.clock(), None, error

        def record(event_name, reason, category="progress", **details):
            self.diagnostics.record(
                event_name,
                stage=stage,
                reason=reason,
                category=category,
                mint=mint,
                started_ns=started_ns,
                creator=event["creator"],
                signature=signature,
                **self.library_details(event["creator"]),
                feed_received_ns=observed_ns,
                candidate_started_ns=started_ns,
                feed_slot=event.get('_feed_slot'),
                chain_timestamp_seconds=event.get('timestamp'),
                **{**metadata_context, **details},
            )

        def metadata_trace(**details):
            metadata_context.update({key: details[key] for key in
                                     ("metadata_host", "metadata_uri_sha256", "metadata_attempt")
                                     if key in details})
            record("CANDIDATE_PROGRESS", **details)

        try:
            record("CANDIDATE_STARTED", "KNOWN_CREATOR_SEED_MATCH")
            chain_ns = int(event["timestamp"]) * 1_000_000_000
            age_ns = self.clock() - chain_ns
            if not 0 <= age_ns < 15_000_000_000:
                stage = "launch_validation"
                record("CANDIDATE_REJECTED", "LAUNCH_OUTSIDE_15_SECOND_WINDOW", "policy")
                return
            early_budget = max(0, self.balance - 30_000_000) * self.position_percent // 100
            if early_budget > 6_005_000 and self.entries_allowed():
                prefetch_request = {
                    "side": "BUY", "mint": mint, "creator": event["creator"],
                    "budget_lamports": str(early_budget), "launch_price": reserve_price(event),
                    "public_key": self.wallet,
                }
                quote_task = asyncio.create_task(prefetch_quote(prefetch_request))
            document, metadata_ns = await self.metadata_requests.fetch(
                metadata, self.metadata_session, event["uri"],
                trace=metadata_trace,
                timeout_seconds=min(3, (15_000_000_000-age_ns)/1e9),
            )
            record("CANDIDATE_PROGRESS", "METADATA_AVAILABLE")
            stage = "social_validation"
            handle, status_ns = social_status(document)
            record(
                "CANDIDATE_PROGRESS",
                "SOCIAL_STATUS_PARSED",
                social_age_ms=(observed_ns - status_ns) / 1e6,
                social_window_ms=10000,
            )
            social_reason = self.policy.social_rejection_reason(
                event["creator"], handle, status_ns, observed_ns)
            if social_reason:
                record("CANDIDATE_REJECTED", social_reason, "policy",
                       social_age_ms=(observed_ns-status_ns)/1e6, social_window_ms=10000)
                return
            stage = "launch_validation"
            create_ns = observed_ns
            if not 0 <= self.clock() - chain_ns <= 15_000_000_000:
                record("CANDIDATE_REJECTED", "LAUNCH_OUTSIDE_15_SECOND_WINDOW", "policy")
                return
            budget = max(0, self.balance - 30_000_000) * self.position_percent // 100
            if budget <= 6_005_000 or not self.entries_allowed():
                record(
                    "CANDIDATE_REJECTED",
                    "BUDGET_OR_ENTRY_BLOCKED",
                    "blocked",
                    blockers=self.entry_blockers(),
                )
                return
            launch = reserve_price(event)
            request = {
                "side": "BUY",
                "mint": mint,
                "creator": event["creator"],
                "budget_lamports": str(budget),
                "launch_price": launch,
                "public_key": self.wallet,
            }
            stage = "quote"
            quote = None
            if quote_task is not None:
                quote_ns, prefetched, error = await quote_task
                # No mutable state cache. An early result is reusable only for
                # this exact budget/request and within 20 ms of its completion.
                # Slow metadata or a balance change forces another current read.
                age = self.clock() - quote_ns
                if prefetch_request == request and 0 <= age <= 20_000_000:
                    if error is not None:
                        raise error
                    quote = prefetched
                    metadata_context["quote_prefetch_reused"] = True
                    metadata_context["quote_prefetch_age_ms"] = age / 1e6
            if quote is None:
                quote = await self.builder.request({**request, "action": "QUOTE"})
            record("CANDIDATE_PROGRESS", "QUOTE_AVAILABLE", **{
                "builder_"+key: value for key, value in quote.get("builder_timings", {}).items()
                if key in BUILDER_TIMINGS and type(value) in (int,float)
                and math.isfinite(value) and 0 <= value <= 120000
            }, **quote_facts(quote), **{
                key: quote[key] for key in ("creator_verification", "curve_creator",
                                            "fee_sharing_admin", "fee_sharing_admin_revoked")
                if key in quote
            })
            if not 0 <= self.clock() - chain_ns <= 15_000_000_000:
                record("CANDIDATE_REJECTED", "LAUNCH_EXPIRED_DURING_QUOTE", "policy")
                return
            stage = "policy"
            facts = EntryFacts(
                event["creator"],
                handle,
                status_ns,
                create_ns,
                self.clock(),
                metadata_ns,
                seed / 1e9,
                False,
                int(quote["quoted_tokens_raw"]),
                int(quote["quoted_tokens_raw"]),
                launch,
                quote["curve_price"],
            )
            decision = self.policy.entry(facts)
            # Exact causal inputs for an offline policy replay; never simulate a fill.
            record('DECISION_EVIDENCE', decision.reason,
                   decision_accepted=decision.accepted, model_sha256=FROZEN_MODEL_SHA256,
                   cash_lamports=self.balance, budget_lamports=budget,
                   occupied_positions=len(self.state.data['positions']),
                   pending_orders=len(self.state.data['pending']),
                   **{'fact_'+key: value for key, value in asdict(facts).items()})
            if not decision.accepted:
                record(
                    "CANDIDATE_REJECTED",
                    decision.reason,
                    "policy",
                    social_age_ms=(create_ns - status_ns) / 1e6,
                    social_window_ms=10000,
                    price_multiple=quote["curve_price"] / launch,
                    maximum_price_multiple=1.5,
                )
                return
            if not self.entries_allowed():
                record(
                    "CANDIDATE_REJECTED",
                    "ENTRY_BLOCKED_AFTER_QUOTE",
                    "blocked",
                    blockers=self.entry_blockers(),
                )
                return
            self.candidates += 1
            record(
                "FROZEN_SIGNAL",
                "FROZEN_PREARMED_SELECTED",
                budget_lamports=budget,
                observed_to_decision_ms=(self.clock() - observed_ns) / 1e6,
                **quote_facts({**quote, 'launch_price': launch,
                               'price_multiple': quote['curve_price']/launch,
                               'maximum_price_multiple': 1.5}),
                creator_seed_lamports=seed,
                social_age_ms=(observed_ns-status_ns)/1e6,
                mode=self.state.data["mode"],
            )
            if self.execution is None:
                record("CANDIDATE_SHADOW_COMPLETE", "SHADOW_NO_TRANSACTION")
                return
            if (
                len(self.state.data["positions"]) >= self.maximum_positions
                or self.state.data["pending"]
            ):
                record("CANDIDATE_REJECTED", "POSITION_OR_PENDING_CAPACITY", "blocked")
                return
            stage = "order_intent"
            request.update(
                expected_tokens_raw=quote["quoted_tokens_raw"],
                submitted_ns=self.clock(),
                expires_ns=chain_ns + 15_000_000_000,
            )
            self.order(
                request,
                {
                    "reference_price": quote["curve_price"],
                    "launch_price": launch,
                    "creator": event["creator"],
                },
                reason="ENTRY",
            )
        except asyncio.CancelledError:
            if quote_task is not None:
                quote_task.cancel()
            raise
        except (KeyError, TypeError, ValueError, RuntimeError, OSError, TimeoutError) as error:
            detail = failure(stage, error)
            category = (
                "policy"
                if detail["reason"]
                in {
                    "SOCIAL_STATUS_URL_REQUIRED",
                    "SOCIAL_STATUS_PATH_INVALID",
                    "CURVE_MIGRATED",
                    "ENTRY_CHASE_GUARD_REJECTED",
                    "ENTRY_OUTPUT_GUARD_REJECTED",
                    "QUOTE_CREATOR_CHANGED",
                    "QUOTE_TOKEN_UNSUPPORTED",
                    "QUOTE_DECIMALS_UNSUPPORTED",
                    "QUOTE_CURRENCY_UNSUPPORTED",
                    "QUOTE_MAYHEM",
                }
                else "fault"
            )
            record("CANDIDATE_REJECTED", detail.pop("reason"), category, **detail)
        finally:
            if quote_task is not None:
                # Drain the bounded read even after metadata rejection so its
                # reply cannot contaminate the next request or cold-start the
                # warmed quote lane. Shutdown cancellation still closes it.
                await asyncio.gather(quote_task, return_exceptions=True)
            self.busy_mints.discard(mint)

    def order(self, payload, context, *, reason):
        request_id = str(uuid.uuid4())
        self.state.data["pending"][request_id] = {
            "payload": payload,
            "context": context,
            "reason": reason,
            "runtime_id": self.diagnostics.run_id,
        }
        self.state.save()  # Durable intent precedes building, signing, or sending.
        self.diagnostics.record(
            "ORDER_INTENT",
            stage="order_intent",
            reason=reason,
            mint=payload["mint"],
            request_id=request_id,
            side=payload["side"],
        )
        self.inflight_orders.add(request_id)
        self.busy_mints.add(payload["mint"])
        self.spawn(self.submit(request_id))
        return request_id

    async def submit(self, request_id):
        order = self.state.data["pending"][request_id]
        try:
            result = await self.execution.execute(request_id, order["payload"])
            self.apply_result(request_id, result)
            if result["status"] == "UNKNOWN":
                self.emit(
                    "EXECUTION_OUTCOME_UNKNOWN",
                    {"request_id": request_id, "mint": order["payload"]["mint"]},
                )
        except Exception as error:  # noqa: BLE001 - Boundary records failure and preserves recovery state.
            self.diagnostics.record(
                "EXECUTION_OUTCOME_UNKNOWN",
                stage="execution",
                category="fault",
                request_id=request_id,
                mint=order["payload"]["mint"],
                **failure("execution", error),
            )
        finally:
            self.inflight_orders.discard(request_id)
            self.busy_mints.discard(order["payload"]["mint"])

    def apply_result(self, request_id, result):
        order = self.state.data["pending"].get(request_id)
        if order is None or result["status"] == "UNKNOWN":
            return
        previous = copy.deepcopy(self.state.data)
        self.state.conn.execute("BEGIN IMMEDIATE")
        try:
            event = self._apply_result(request_id, result)
            event = self.state.reports.record_result(
                request_id, order, result, previous, self.state.data, event, self.clock())
            self.state.conn.execute("COMMIT")
        except Exception:
            self.state.conn.execute("ROLLBACK")
            self.state.data = previous
            self.failures = self.state.data["failed_order_ns"]
            raise
        # Audit/notification failures cannot cause a confirmed fill to be applied twice.
        self.emit(*event)
        if event[0] == "FILL_CONFIRMED" and event[1].get("recovery_exit_reason"):
            self.diagnostics.record("POSITION_RECOVERY_EXIT", stage="exit", category="fault",
                                    reason=event[1]["recovery_exit_reason"],
                                    mint=event[1]["mint"], request_id=request_id)

    def _apply_result(self, request_id, result):
        order = self.state.data["pending"][request_id]
        payload, context = order["payload"], order["context"]
        mint, side = payload["mint"], payload["side"]
        if result["status"] == "FAILED":
            fee = result.get("fee_lamports", 0)
            if type(fee) is not int or fee < 0:
                raise RuntimeError("INVALID_FAILED_TRANSACTION_FEE")
            self.state.data["failed_transaction_fees_lamports"] = (
                self.state.data.get("failed_transaction_fees_lamports", 0) + fee
            )
            if side == "SELL" and mint in self.state.data["positions"]:
                self.state.data["positions"][mint]["pnl_lamports"] -= fee
            expected_rejection = side == "BUY" and result.get("reason") in {
                "ENTRY_NO_LONGER_ALLOWED",
                "BUILDER:CURVE_MIGRATED",
                "ENTRY_EXPIRED",
                "BUILDER:ENTRY_CHASE_GUARD_REJECTED",
                "BUILDER:ENTRY_OUTPUT_GUARD_REJECTED",
            }
            if not expected_rejection:
                self.failures.append(self.clock())
            event = (
                "ORDER_FAILED",
                {
                    "mint": mint,
                    "side": side,
                    "reason": result.get("reason"),
                    "request_id": request_id,
                    "fee_lamports": fee,
                },
            )
        elif result["status"] == "CONFIRMED":
            raw, delta = result["tokens_raw"], result["sol_delta_lamports"]
            if type(raw) is not int or raw <= 0 or type(delta) is not int:
                raise RuntimeError("INVALID_CONFIRMED_FILL")
            if side == "BUY":
                reference = result.get("reference_price")
                forced_exit = reference is None or not math.isfinite(reference) or reference <= 0
                if forced_exit:
                    reference = context["reference_price"]
                    self.breaker.exit_only("buy_reference_missing")
                elif reference > context["launch_price"] * 1.5:
                    forced_exit = True
                    self.breaker.exit_only("price_moved_beyond_guard_before_landing")
                self.state.data["positions"][mint] = {
                    "original": raw,
                    "remaining": raw,
                    "pnl_lamports": delta,
                    "exit": asdict(ExitState(payload["submitted_ns"], reference, reference)),
                    "latest_price": reference,
                    "creator": context["creator"],
                    "forced_exit": forced_exit or bool(context.get("test_operation_id")),
                    "test_operation_id": context.get("test_operation_id"),
                    "last_attempt_ns": 0,
                    "entry_signature": result.get("signature"),
                }
                self.restore_buy_market(order, result, self.state.data["positions"][mint])
            else:
                position = self.state.data["positions"][mint]
                if raw > position["remaining"] or raw != int(payload["tokens_raw"]):
                    self.breaker.halt("unexpected_sell_token_delta")
                    raise RuntimeError("UNEXPECTED_SELL_TOKEN_DELTA")
                position["remaining"] -= raw
                position["pnl_lamports"] += delta
                # A confirmed sale is complete. Backoff belongs to failed attempts;
                # it must not delay the next policy exit after a partial fill.
                position["last_attempt_ns"] = 0
                if order["reason"] == "FIRST_TAKE":
                    position["exit"]["first_partial_done"] = True
                    if position.get("exit_latched_reason") == "FIRST_TAKE":
                        position.pop("exit_latched_reason", None)
                    # A stop can be crossed during partial confirmation. Re-evaluate
                    # the latest observed price with the now-confirmed partial floor.
                    self.update_exit_state(position, self.clock(), price=position["latest_price"])
                if position["remaining"] == 0:
                    if position.get("test_operation_id"):
                        self.state.data["test_closed"] = self.state.data.get("test_closed", 0) + 1
                        self.state.data["test_realized_lamports"] = (
                            self.state.data.get("test_realized_lamports", 0)
                            + position["pnl_lamports"]
                        )
                    else:
                        self.state.data["closed"] += 1
                        self.state.data["realized_lamports"] += position["pnl_lamports"]
                        self.safety.record_trade_result(position["pnl_lamports"] / 1e9)
                    del self.state.data["positions"][mint]
            event = (
                "FILL_CONFIRMED",
                {
                    "mint": mint,
                    "side": side,
                    "tokens_raw": raw,
                    "signature": result.get("signature"),
                    "request_id": request_id,
                    **({key: self.state.data["positions"][mint].get(key) for key in
                        ("fill_clock_source", "receipt_observed_ns", "replayed_market_events", "pending_market_events",
                         "recovery_exit_reason", "exit_latched_reason")}
                       | {"fill_observed_ns": self.state.data["positions"][mint]["exit"]["fill_ns"],
                          "hold_deadline_ns": self.state.data["positions"][mint]["exit"]["fill_ns"]
                          + self.policy.hold_ns}
                       if side == "BUY" else {}),
                },
            )
        else:
            raise RuntimeError("UNRECOGNIZED_EXECUTION_OUTCOME")
        del self.state.data["pending"][request_id]
        # Refresh even after a chain rejection, which can consume a fee. Wake
        # the single balance poller instead of waiting for its one-second tick.
        self.invalidate_balance()
        self.state.save()
        return event

    def consider_exit(self, mint, observed_ns, *, price=None, complete=False, timer_only=False):
        position = self.state.data["positions"][mint]
        update = self.update_exit_state(position, observed_ns, price=price,
                                        complete=complete, timer_only=timer_only)
        self.state.save()
        if self.execution is None or any(
            o["payload"]["mint"] == mint for o in self.state.data["pending"].values()
        ):
            return
        forced = (
            self.stopping
            or self.kill_path.exists()
            or position["forced_exit"]
            or not self.safety.snapshot().entries_allowed
        )
        actions = update.actions
        latched = position.get("exit_latched_reason")
        if not actions and not forced and not latched:
            return
        if observed_ns - position["last_attempt_ns"] < 1_000_000_000:
            return
        # Multiple simultaneous policy actions can be fulfilled by one full sale.
        partial = not forced and latched == "FIRST_TAKE"
        amount = (
            min(position["remaining"], position["original"] * 30 // 100)
            if partial
            else position["remaining"]
        )
        if amount <= 0:
            return
        position["last_attempt_ns"] = observed_ns
        reason = (
            "OPERATOR_OR_SAFETY_EXIT"
            if forced
            else latched
        )
        self.order(
            {
                "side": "SELL",
                "mint": mint,
                "creator": position["creator"],
                "tokens_raw": str(amount),
                "submitted_ns": self.clock(),
            },
            {},
            reason=reason,
        )

    async def reconcile(self):
        for request_id, order in list(self.state.data["pending"].items()):
            if request_id in self.inflight_orders:
                continue
            self.inflight_orders.add(request_id)
            self.busy_mints.add(order["payload"]["mint"])
            try:
                entry = self.execution.journal.by_request(request_id)
                if entry is None or entry.state == "PREPARED":
                    # Nothing was durably signed. Never replay an old entry decision.
                    result = {"status": "FAILED", "reason": "INTERRUPTED_BEFORE_SIGNING"}
                else:
                    result = await self.execution.resolve(entry)
                self.apply_result(request_id, result)
            finally:
                self.inflight_orders.discard(request_id)
                self.busy_mints.discard(order["payload"]["mint"])

    async def tick(self):
        now = self.clock()
        self.failures = [n for n in self.failures if now - n < 60_000_000_000]
        if self.state.data["failed_order_ns"] != self.failures:
            self.state.data["failed_order_ns"] = self.failures
            self.state.save()
        else:
            self.state.data["failed_order_ns"] = self.failures
        self.safety.set_tx_failures(len(self.failures))
        self.breaker.evaluate_operational()
        snapshot = self.safety.snapshot()
        safety = (snapshot.mode.value, snapshot.reason)
        if safety != self.last_safety:
            self.last_safety = safety
            self.emit("SAFETY_MODE_CHANGED", {"mode": safety[0], "reason": safety[1]})
        for mint in list(self.state.data["positions"]):
            self.consider_exit(mint, now, timer_only=True)
        if self.execution and now - self.last_reconcile_ns >= 2_000_000_000:
            self.last_reconcile_ns = now
            self.spawn(self.reconcile())

    def entry_blockers(self):
        now = self.clock()
        checks = (
            (self.stopping, "STOP_REQUESTED"),
            (not self.connected, "FEED_DISCONNECTED"),
            (not 0 <= now - self.last_feed_ns < 5_000_000_000, "FEED_STALE"),
            (not 0 <= now - self.balance_ns < 5_000_000_000, "BALANCE_STALE"),
            (self.balance <= 30_000_000, "BALANCE_BELOW_RESERVE"),
            (self.kill_path.exists(), "KILL_SWITCH"),
            (
                not self.safety.snapshot().entries_allowed,
                "SAFETY_" + self.safety.snapshot().reason.upper(),
            ),
            (bool(self.state.data["pending"]), "UNRESOLVED_ORDER"),
        )
        return [reason for blocked, reason in checks if blocked]

    def library_details(self, creator):
        if self.creator_library is None:
            return {}
        return {k: v for k, v in self.creator_library.describe(creator).items() if k != "creator"}

    def status(self):
        return {
            "mode": self.state.data["mode"],
            "feed_connected": self.connected,
            "feed_events": self.feed_events,
            "signals": self.candidates,
            "positions": len(self.state.data["positions"]),
            "unresolved_orders": len(self.state.data["pending"]),
            "entries_allowed": self.entries_allowed(),
            "safety_mode": self.safety.snapshot().mode.value,
            "safety_reason": self.safety.snapshot().reason,
            "closed_trades": self.state.data["closed"],
            "net_realized_sol": self.state.data["realized_lamports"] / 1e9,
            "updated_ns": self.clock(),
            "balance_updated_ns": self.balance_ns,
            "balance_age_ms": max(0, (self.clock() - self.balance_ns) / 1e6) if self.balance_ns else None,
            "balance_refresh_started_ns": self.balance_refresh_started_ns,
            "balance_refresh_duration_ms": self.balance_refresh_duration_ms,
            "balance_refresh_inflight": self.balance_refresh_inflight,
            "entry_blockers": self.entry_blockers(),
            "position_percent": self.position_percent,
            "maximum_positions": self.maximum_positions,
            "source_sha256": getattr(self, "source_sha256", None),
            "provider_health": self.rpc.snapshot() if hasattr(self.rpc, "snapshot") else [],
            "discovery": self.rpc.discovery_budget.snapshot()
            if getattr(self.rpc, "discovery_budget", None) is not None else {},
            "diagnostics": self.diagnostics.snapshot(),
            "creator_library": self.creator_library.status()
            if self.creator_library
            else {"state": "NOT_CONNECTED"},
        }
