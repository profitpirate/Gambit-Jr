# V1.5 realtime intelligence fabric

## Runtime truth

The canonical fabric persists an event before projection or model consumption. It owns
cross-provider identity, confirmation sources, semantic conflicts, processing leases,
three timestamps, provenance, and downstream latency checkpoints. Existing `tokens` and
`candidates` remain authoritative; the fabric does not create a second candidate universe.

Pump program `logsSubscribe` is the default no-key launch/trade stream. Confirmed
transactions are decoded from the current Pump Anchor layouts, and active bonding-curve
accounts receive both an initial `getAccountInfo` read and dynamic `accountSubscribe`
updates. Current quote-aware accounts retain quote units; real-SOL fields are populated
only for SOL/WSOL layouts. The implementation follows the current public Pump IDL in
[pump-public-docs](https://github.com/pump-fun/pump-public-docs).

PumpPortal is optional redundancy and uses one socket for only `subscribeNewToken` and
`subscribeMigration`, consistent with the current [PumpPortal data API
documentation](https://pumpportal.fun/data-api/real-time/). Paid token/account trade
subscriptions are not enabled.

Selective Helius monitoring deliberately uses standard Solana `logsSubscribe` plus
`getTransaction`, one mention per configured account. It does not depend on enhanced
paid `transactionSubscribe`. Native Pump logs remain the fallback if Helius is absent.
See the official [Helius WebSocket guide](https://www.helius.dev/docs/rpc/websocket) and
[Solana logsSubscribe RPC](https://solana.com/docs/rpc/websocket/logssubscribe).

Jito tip-account transfers are recorded as probabilistic bundle evidence, never an exact
bundle ID or a malicious verdict. Tip accounts should be periodically checked against
Jito's official [`getTipAccounts`](https://docs.jito.wtf/lowlatencytxnsend/).

## Feature truth

The runtime feature store contains the exact bands 0–15, 15–30, 30–60, 60–90,
90–120, 120–180, 180–300, 300–600, and 600–1800 seconds. It retains buyer arrival,
flow, first/meaningful sells, sell absorption, raw and adjusted activity, real-reserve
derivatives, curve progress, migration continuity, actor evidence, monitoring temperature,
and explicit coverage. Independence remains `UNKNOWN` until a linkage/funder graph exists.

GENESIS/HOT/WARM/COLD/DEAD state controls `next_monitor_at`. Market fallback reads use
DexScreener's multi-token endpoint in chunks of 30. BNB factory discovery is websocket
first when the configured RPC supports it; persisted cursor-based `eth_getLogs` handles
recovery and non-websocket endpoints.

Operator `/token` output reads the same persisted trajectory used by the shadow decision
ledger. Mature outcomes feed wallet copyability profiles and the explicit
`realtime-research` challenger command. Hypotheses and challengers never set a public
route or gain `APPROVED` state automatically.

## Historical evidence boundary

`scripts/v15_realtime_replay.py` reconstructs every valid ordered transaction event and
all nine bands from the licensed local Pump.fun corpus. It records raw buyer identities,
first sells, post-sell buyers, flow, and creator-linked activity. The corpus lacks native
bonding-curve account history, point-in-time funder edges, provider arrival latency, exact
bundle identity, and dynamic social/narrative observations. Those fields remain explicitly
unavailable and are never proxied with market cap or virtual reserves.

`scripts/v15_realtime_research.py` fits only a research challenger and development-fitted
CONTROL×realtime hybrid. It reports fixed-frequency outcomes, Wilson intervals, a
quantitative low-performance autopsy, and hypotheses. Previously inspected June/July
windows are marked retired and cannot approve a model. A genuinely later prospective
window and live shadow coverage are required before human promotion can even be considered.

## Operations

Provider states are `CONNECTED`, `DEGRADED`, `STALE`, `DISCONNECTED`, `RECOVERING`, or
`NOT_CONFIGURED`. A socket silence timeout forces stale/disconnected health and reconnect.
Native Solana recovers bounded signature gaps; BNB recovers block cursor gaps. Processing
leases recover after a crash, while central keys suppress duplicate providers and replay
storms.

This work does not deploy production. PumpPortal, Helius, verified BNB factories, dynamic
social/narrative providers, and sustained live shadow capture require operator-provided
configuration and must be reported as not configured or blocked until real coverage exists.
