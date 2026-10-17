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

from .v12_prearmed_evidence import FROZEN_MODEL_SHA256
from .v12_prearmed_io import metadata
from .v12_prearmed_policy import EntryFacts, ExitState, FrozenPrearmedPolicy
from .v12_safety import SafetyStore, CircuitBreaker


def social_status(document):
    extensions = document.get('extensions') or {}
    if not isinstance(extensions, dict):
        raise ValueError('metadata extensions must be an object')
    value = document.get('twitter') or extensions.get('twitter', '')
    url = urlsplit(str(value))
    if url.scheme != 'https' or url.hostname not in {'x.com', 'www.x.com', 'twitter.com', 'www.twitter.com'}:
        raise ValueError('metadata requires a public X/Twitter status URL')
    match = re.fullmatch(r'/([A-Za-z0-9_]{1,15})/status/([0-9]{10,22})/?', url.path)
    if not match:
        raise ValueError('status URL missing')
    snowflake = int(match[2])
    return match[1].lower(), ((snowflake >> 22) + 1288834974657) * 1_000_000


def reserve_price(event):
    sol, tokens = event.get('virtual_sol_reserves'), event.get('virtual_token_reserves')
    if type(sol) is not int or type(tokens) is not int or sol <= 0 or tokens <= 0:
        raise ValueError('invalid reserves')
    return sol / tokens / 1000


class State:
    def __init__(self, path, *, wallet, mode):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, isolation_level=None)
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.execute('PRAGMA synchronous=FULL')
        self.conn.execute('CREATE TABLE IF NOT EXISTS frozen_state (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL)')
        self.conn.execute('CREATE TABLE IF NOT EXISTS frozen_seen (mint TEXT PRIMARY KEY)')
        row = self.conn.execute('SELECT data FROM frozen_state WHERE id=1').fetchone()
        self.data = json.loads(row[0]) if row else dict(wallet=wallet, mode=mode, model=FROZEN_MODEL_SHA256,
            positions={}, pending={}, closed=0, realized_lamports=0)
        if any(self.data[k] != v for k, v in dict(wallet=wallet, mode=mode, model=FROZEN_MODEL_SHA256).items()):
            raise RuntimeError('STATE_WALLET_MODE_OR_MODEL_MISMATCH')
        self.save()

    def save(self):
        self.conn.execute('INSERT OR REPLACE INTO frozen_state VALUES(1,?)',
                          (json.dumps(self.data, allow_nan=False, separators=(',', ':')),))

    def reserve_mint(self, mint):
        return self.conn.execute('INSERT OR IGNORE INTO frozen_seen VALUES(?)', (mint,)).rowcount == 1


class Runtime:
    def __init__(self, *, model, state, rpc, builder, execution, wallet, metadata_session, emit,
                 kill_path, clock=time.time_ns):
        self.model, self.policy, self.state = model, FrozenPrearmedPolicy(model), state
        self.rpc, self.builder, self.execution, self.wallet = rpc, builder, execution, wallet
        self.metadata_session, self.emit = metadata_session, emit
        self.kill_path, self.clock = Path(kill_path), clock
        self.safety = SafetyStore(state.conn)
        self.breaker = CircuitBreaker(self.safety)
        snapshot = self.safety.snapshot()
        self.last_safety = (snapshot.mode.value, snapshot.reason)
        self.connected, self.last_feed_ns, self.balance = False, 0, 0
        self.balance_ns, self.stopping = 0, False
        self.busy_mints, self.tasks, self.signatures, self.inflight_orders = set(), set(), {}, set()
        self.sequence, self.feed_events, self.candidates = 0, 0, 0
        self.failures = self.state.data.setdefault('failed_order_ns', [])
        self.last_reconcile_ns = 0
        if self.execution is not None:
            self.execution.allow_buy = self.entry_conditions

    def spawn(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        def done(t):
            self.tasks.discard(t)
            if not t.cancelled() and t.exception() is not None:
                self.breaker.exit_only('runtime_task_failed')
                self.emit('RUNTIME_TASK_FAILED', {})
        task.add_done_callback(done)
        return task

    def feed_health(self, connected):
        self.connected = connected

    def entry_conditions(self):
        now = self.clock()
        return (not self.stopping and self.connected and now - self.last_feed_ns < 5_000_000_000
                and now - self.balance_ns < 5_000_000_000 and self.balance > 30_000_000
                and not self.kill_path.exists() and self.safety.snapshot().entries_allowed)

    def entries_allowed(self):
        return self.entry_conditions() and not self.state.data['pending']

    async def refresh_balance(self):
        self.balance = await self.rpc.balance_lamports(self.wallet)
        self.balance_ns = self.clock()
        # Closed cash-equity checks avoid attributing the whole entry cost as a loss.
        # Open positions still have independent stop/hold exits and cannot add risk.
        if not self.state.data['positions'] and not self.state.data['pending']:
            self.breaker.evaluate_equity(self.balance / 1e9)
        self.breaker.evaluate_operational()

    async def on_batch(self, signature, events, observed_ns):
        if signature in self.signatures:
            return
        self.signatures[signature] = observed_ns
        if len(self.signatures) > 10_000:
            self.signatures.pop(next(iter(self.signatures)))
        self.last_feed_ns = observed_ns
        self.feed_events += len(events)
        for event in events:
            kind, mint = event.get('anchor_event'), event.get('mint')
            self.sequence += 1
            position = self.state.data['positions'].get(mint)
            if position and kind in {'TradeEvent', 'CompleteEvent', 'CompletePumpAmmMigrationEvent'}:
                try:
                    price = reserve_price(event) if kind == 'TradeEvent' else position['latest_price']
                    self.consider_exit(mint, observed_ns, price=price, complete=kind != 'TradeEvent')
                except ValueError:
                    self.breaker.exit_only('invalid_position_market_data')
            if kind != 'CreateEvent' or not self.entries_allowed():
                continue
            creator = event.get('creator')
            if creator not in self.model['creator_handles'] or mint in self.busy_mints:
                continue
            if event.get('is_mayhem_mode') is not False or event.get('quote_mint') not in (
                    None, '11111111111111111111111111111111', 'So11111111111111111111111111111111111111112'):
                continue
            seed = sum(e.get('sol_amount', 0) for e in events if e.get('anchor_event') == 'TradeEvent'
                       and e.get('mint') == mint and e.get('user') == creator and e.get('is_buy') is True)
            if seed < 2_000_000_000:
                continue
            if len(self.state.data['positions']) + len(self.busy_mints) >= 2:
                continue
            if not self.state.reserve_mint(mint):
                continue
            self.busy_mints.add(mint)
            self.spawn(self.enter(event, seed, observed_ns))

    async def enter(self, event, seed, observed_ns):
        mint = event['mint']
        try:
            document, metadata_ns = await metadata(self.metadata_session, event['uri'])
            handle, status_ns = social_status(document)
            create_ns = observed_ns
            chain_ns = int(event['timestamp']) * 1_000_000_000
            if not 0 <= self.clock() - chain_ns <= 15_000_000_000:
                return  # Reject old/replayed launches; do not backfill entries.
            budget = max(0, self.balance - 30_000_000) // 10
            if budget <= 6_005_000 or not self.entries_allowed():
                return
            launch = reserve_price(event)
            request = dict(side='BUY', mint=mint, creator=event['creator'], budget_lamports=str(budget),
                           launch_price=launch, public_key=self.wallet)
            quote = await self.builder.request({**request, 'action': 'QUOTE'})
            facts = EntryFacts(event['creator'], handle, status_ns, create_ns, self.clock(), metadata_ns,
                seed / 1e9, False, int(quote['quoted_tokens_raw']), int(quote['quoted_tokens_raw']),
                launch, quote['curve_price'])
            decision = self.policy.entry(facts)
            if not decision.accepted or not self.entries_allowed():
                return
            self.candidates += 1
            self.emit('FROZEN_SIGNAL', dict(mint=mint, budget_lamports=budget,
                observed_to_decision_ms=(self.clock() - observed_ns) / 1e6, mode=self.state.data['mode']))
            if self.execution is None:
                return  # Shadow observes/quotes only; it never manufactures fills.
            if len(self.state.data['positions']) >= 2 or self.state.data['pending']:
                return
            request.update(expected_tokens_raw=quote['quoted_tokens_raw'], submitted_ns=self.clock())
            self.order(request, dict(reference_price=quote['curve_price'], launch_price=launch,
                creator=event['creator']), reason='ENTRY')
        except (KeyError, TypeError, ValueError, RuntimeError, OSError, TimeoutError):
            self.emit('CANDIDATE_REJECTED', {'mint': mint, 'reason': 'DATA_OR_QUOTE_UNAVAILABLE'})
        finally:
            self.busy_mints.discard(mint)

    def order(self, payload, context, *, reason):
        request_id = str(uuid.uuid4())
        self.state.data['pending'][request_id] = dict(payload=payload, context=context, reason=reason)
        self.state.save()  # Durable intent precedes building, signing, or sending.
        self.inflight_orders.add(request_id)
        self.busy_mints.add(payload['mint'])
        self.spawn(self.submit(request_id))
        return request_id

    async def submit(self, request_id):
        order = self.state.data['pending'][request_id]
        try:
            result = await self.execution.execute(request_id, order['payload'])
            self.apply_result(request_id, result)
            if result['status'] == 'UNKNOWN':
                self.emit('EXECUTION_OUTCOME_UNKNOWN', {'request_id': request_id, 'mint': order['payload']['mint']})
        except Exception:
            self.emit('EXECUTION_OUTCOME_UNKNOWN', {'request_id': request_id})
        finally:
            self.inflight_orders.discard(request_id)
            self.busy_mints.discard(order['payload']['mint'])

    def apply_result(self, request_id, result):
        order = self.state.data['pending'].get(request_id)
        if order is None or result['status'] == 'UNKNOWN':
            return
        previous = copy.deepcopy(self.state.data)
        self.state.conn.execute('BEGIN IMMEDIATE')
        try:
            event = self._apply_result(request_id, result)
            self.state.conn.execute('COMMIT')
        except Exception:
            self.state.conn.execute('ROLLBACK')
            self.state.data = previous
            self.failures = self.state.data['failed_order_ns']
            raise
        # Audit/notification failures cannot cause a confirmed fill to be applied twice.
        self.emit(*event)

    def _apply_result(self, request_id, result):
        order = self.state.data['pending'][request_id]
        payload, context = order['payload'], order['context']
        mint, side = payload['mint'], payload['side']
        if result['status'] == 'FAILED':
            expected_rejection = side == 'BUY' and result.get('reason') in {
                'ENTRY_NO_LONGER_ALLOWED', 'BUILDER:CURVE_MIGRATED',
                'BUILDER:ENTRY_CHASE_GUARD_REJECTED', 'BUILDER:ENTRY_OUTPUT_GUARD_REJECTED'}
            if not expected_rejection:
                self.failures.append(self.clock())
            event = ('ORDER_FAILED', dict(mint=mint, side=side, reason=result.get('reason')))
        elif result['status'] == 'CONFIRMED':
            raw, delta = result['tokens_raw'], result['sol_delta_lamports']
            if type(raw) is not int or raw <= 0 or type(delta) is not int:
                raise RuntimeError('INVALID_CONFIRMED_FILL')
            if side == 'BUY':
                reference = result.get('reference_price')
                forced_exit = reference is None or not math.isfinite(reference) or reference <= 0
                if forced_exit:
                    reference = context['reference_price']
                    self.breaker.exit_only('buy_reference_missing')
                elif reference > context['launch_price'] * 1.5:
                    forced_exit = True
                    self.breaker.exit_only('price_moved_beyond_guard_before_landing')
                fill = payload['submitted_ns']  # Conservative deadline: never restarted at receipt time.
                self.state.data['positions'][mint] = dict(original=raw, remaining=raw, pnl_lamports=delta,
                    exit=asdict(ExitState(fill, reference, reference)), latest_price=reference,
                    creator=context['creator'], forced_exit=forced_exit, last_attempt_ns=0)
            else:
                position = self.state.data['positions'][mint]
                if raw > position['remaining'] or raw != int(payload['tokens_raw']):
                    self.breaker.halt('unexpected_sell_token_delta')
                    raise RuntimeError('UNEXPECTED_SELL_TOKEN_DELTA')
                position['remaining'] -= raw
                position['pnl_lamports'] += delta
                if order['reason'] == 'FIRST_TAKE':
                    position['exit']['first_partial_done'] = True
                if position['remaining'] == 0:
                    self.state.data['closed'] += 1
                    self.state.data['realized_lamports'] += position['pnl_lamports']
                    self.safety.record_trade_result(position['pnl_lamports'] / 1e9)
                    del self.state.data['positions'][mint]
            event = ('FILL_CONFIRMED', dict(mint=mint, side=side, tokens_raw=raw, signature=result.get('signature')))
        else:
            raise RuntimeError('UNRECOGNIZED_EXECUTION_OUTCOME')
        del self.state.data['pending'][request_id]
        self.balance_ns = 0  # Refresh even after chain rejection, which can consume a fee.
        self.state.save()
        return event

    def consider_exit(self, mint, observed_ns, *, price=None, complete=False, timer_only=False):
        position = self.state.data['positions'][mint]
        current = ExitState(**position['exit'])
        update = self.policy.exits(current, observed_ns=observed_ns, price=price,
                                   complete=complete, timer_only=timer_only, sequence=self.sequence)
        # Peak/observation can advance immediately; the partial/closed flags require receipt.
        acknowledged = replace(update.state, first_partial_done=current.first_partial_done, closed=False)
        position['exit'] = asdict(acknowledged)
        if price is not None:
            position['latest_price'] = price
        self.state.save()
        if self.execution is None or any(o['payload']['mint'] == mint for o in self.state.data['pending'].values()):
            return
        forced = self.stopping or self.kill_path.exists() or position['forced_exit'] or not self.safety.snapshot().entries_allowed
        actions = update.actions
        if not actions and not forced:
            return
        if observed_ns - position['last_attempt_ns'] < 1_000_000_000:
            return
        # Multiple simultaneous policy actions can be fulfilled by one full sale.
        partial = not forced and len(actions) == 1 and actions[0].reason == 'FIRST_TAKE'
        amount = min(position['remaining'], position['original'] * 30 // 100) if partial else position['remaining']
        if amount <= 0:
            return
        position['last_attempt_ns'] = observed_ns
        reason = 'OPERATOR_OR_SAFETY_EXIT' if forced else (actions[0].reason if partial else actions[-1].reason)
        self.order(dict(side='SELL', mint=mint, creator=position['creator'], tokens_raw=str(amount),
                        submitted_ns=self.clock()), {}, reason=reason)

    async def reconcile(self):
        for request_id, order in list(self.state.data['pending'].items()):
            if request_id in self.inflight_orders:
                continue
            self.inflight_orders.add(request_id)
            self.busy_mints.add(order['payload']['mint'])
            try:
                entry = self.execution.journal.by_request(request_id)
                if entry is None or entry.state == 'PREPARED':
                    # Nothing was durably signed. Never replay an old entry decision.
                    result = {'status': 'FAILED', 'reason': 'INTERRUPTED_BEFORE_SIGNING'}
                else:
                    result = await self.execution.resolve(entry)
                self.apply_result(request_id, result)
            finally:
                self.inflight_orders.discard(request_id)
                self.busy_mints.discard(order['payload']['mint'])

    async def tick(self):
        now = self.clock()
        self.failures = [n for n in self.failures if now - n < 60_000_000_000]
        if self.state.data['failed_order_ns'] != self.failures:
            self.state.data['failed_order_ns'] = self.failures
            self.state.save()
        else:
            self.state.data['failed_order_ns'] = self.failures
        self.safety.set_tx_failures(len(self.failures))
        self.breaker.evaluate_operational()
        snapshot = self.safety.snapshot()
        safety = (snapshot.mode.value, snapshot.reason)
        if safety != self.last_safety:
            self.last_safety = safety
            self.emit('SAFETY_MODE_CHANGED', dict(mode=safety[0], reason=safety[1]))
        for mint in list(self.state.data['positions']):
            self.consider_exit(mint, now, timer_only=True)
        if self.execution and now - self.last_reconcile_ns >= 2_000_000_000:
            self.last_reconcile_ns = now
            self.spawn(self.reconcile())

    def status(self):
        return dict(mode=self.state.data['mode'], feed_connected=self.connected, feed_events=self.feed_events,
            signals=self.candidates, positions=len(self.state.data['positions']),
            unresolved_orders=len(self.state.data['pending']), entries_allowed=self.entries_allowed(),
            safety_mode=self.safety.snapshot().mode.value, safety_reason=self.safety.snapshot().reason,
            closed_trades=self.state.data['closed'], net_realized_sol=self.state.data['realized_lamports'] / 1e9,
            updated_ns=self.clock())
