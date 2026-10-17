"""Observation only: bounded timings never admit, reject or alter an order."""
from collections import deque
import math

METRICS = frozenset({
    'feed_decode_ms', 'feed_dispatch_ms', 'candidate_queue_ms',
    'create_age_approx_ms', 'event_loop_lag_ms', 'balance_refresh_ms',
    'metadata_attempt_ms', 'metadata_dns_ms', 'metadata_connect_ms',
    'metadata_ttfb_ms', 'metadata_body_ms', 'observed_to_decision_ms',
    'quote_state_read_ms', 'quote_total_ms', 'quote_rpc_route_ms',
    'build_state_read_ms', 'build_total_ms', 'build_simulation_ms',
    'build_blockhash_ready_ms', 'build_balance_read_ms', 'build_rpc_route_ms',
})
BUILDER_TIMINGS = frozenset({'state_read_ms', 'total_ms', 'simulation_ms',
                            'blockhash_ready_ms', 'balance_read_ms', 'rpc_route_ms'})


class Timings:
    def __init__(self):
        self.samples = {}
        self.totals = {}
        self.latest = {}
        self.head_slot = self.head_ns = 0
        self.feed_slot = self.feed_ns = 0
        self.lag_since_ns = 0

    def observe(self, name, value, now):
        if (name not in METRICS or type(value) not in (int, float)
                or not math.isfinite(value) or not 0 <= value <= 86_400_000):
            return
        self.samples.setdefault(name, deque(maxlen=512)).append(float(value))
        self.totals[name] = self.totals.get(name, 0) + 1
        self.latest[name] = now

    def head(self, slot, now):
        if type(slot) is int and 0 < slot < 2**53:
            if not 0 <= now-self.head_ns <= 5_000_000_000:
                self.lag_since_ns = 0
            self.head_slot, self.head_ns = slot, now

    def feed(self, event, observed_ns, now):
        received = event.get('_feed_received_ns', observed_ns)
        decoded = event.get('_feed_decoded_ns', observed_ns)
        if type(received) is int and type(decoded) is int:
            self.observe('feed_decode_ms', (decoded - received) / 1e6, now)
            self.observe('feed_dispatch_ms', (now - decoded) / 1e6, now)
        slot = event.get('_feed_slot')
        if type(slot) is int and 0 < slot < 2**53:
            if not 0 <= now-self.feed_ns <= 5_000_000_000:
                self.lag_since_ns = 0
            self.feed_slot, self.feed_ns = slot, now
            reference_fresh = 0 <= now - self.head_ns <= 5_000_000_000
            if reference_fresh and self.head_slot - slot >= 8:
                self.lag_since_ns = self.lag_since_ns or now
            else:
                self.lag_since_ns = 0
        if event.get('anchor_event') == 'CreateEvent':
            stamp = event.get('timestamp')
            if type(stamp) is int:
                # Chain time is whole seconds: this is NOT exact delivery latency.
                self.observe('create_age_approx_ms', (observed_ns-stamp*1e9)/1e6, now)

    def snapshot(self, now):
        metrics = {}
        for name, values in self.samples.items():
            ordered = sorted(values)
            metrics[name] = dict(count=self.totals[name], samples=len(ordered),
                latest_ns=self.latest[name], last=values[-1], max=ordered[-1],
                **{f'p{p}': ordered[max(0, math.ceil(len(ordered)*p/100)-1)] for p in (50, 95, 99)})
        fresh = (0 < self.head_ns and 0 < self.feed_ns
                 and 0 <= now-self.head_ns <= 5_000_000_000
                 and 0 <= now-self.feed_ns <= 5_000_000_000)
        lag = max(0, self.head_slot-self.feed_slot) if fresh else None
        return {'window_samples': 512, 'metrics': metrics, 'feed': {
            'reference': 'EXISTING_CONFIRMED_BALANCE_READ',
            'chain_age_resolution_ms': 1000, 'head_slot': self.head_slot,
            'head_observed_ns': self.head_ns, 'feed_slot': self.feed_slot,
            'feed_observed_ns': self.feed_ns, 'reference_fresh': fresh,
            'lag_slots': lag, 'lag_since_ns': self.lag_since_ns,
            'sustained_lag': bool(fresh and lag >= 8 and self.lag_since_ns
                                 and now-self.lag_since_ns >= 30_000_000_000),
        }}
