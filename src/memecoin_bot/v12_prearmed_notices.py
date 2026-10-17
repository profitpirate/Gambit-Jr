"""Human-readable event notices with explicit urgency and no raw error strings."""

from datetime import datetime
from zoneinfo import ZoneInfo

UK = ZoneInfo("Europe/London")


def sol(lamports, *, signed=False):
    return f"{lamports / 1e9:+.6f}" if signed else f"{lamports / 1e9:.6f}"


def readable(value):
    value = str(value or "unknown")
    if "://" in value or len(value) > 100:
        return "See /bot logs for the recorded reason"
    return " ".join(value.replace("_", " ").split()).lower()


def stamp(ts_ns):
    return datetime.fromtimestamp(ts_ns / 1e9, UK).strftime("%d %b %Y · %H:%M:%S %Z")


def format_daily(report, *, now_ns):
    net = report['net_pnl_lamports']
    icon = "🟢" if net > 0 else "🔴" if net < 0 else "⚪"
    lines = [f"📊 Gambit daily P/L · {report['day']} (UK)",
             f"{icon} Net for the day: {sol(net, signed=True)} SOL",
             f"Trades closed: {report['closed']} · {report['wins']} wins / {report['losses']} losses / {report['breakeven']} flat"]
    if report['closed']:
        lines.append(f"Win rate: {report['wins']/report['closed']:.0%} · Best {sol(report['best_lamports'], signed=True)} / worst {sol(report['worst_lamports'], signed=True)} SOL")
    else:
        lines.append("No bot trades closed today.")
    lines += [f"All-time closed-trade P/L: {sol(report['all_time_pnl_lamports'], signed=True)} SOL · {report['all_time_closed']} trades",
              f"Open positions: {report['open_positions_now']} · Waiting for confirmation: {report['pending_orders_now']}",
              "Closed bot trades only; open positions and manual trades excluded."]
    if report['failed_buy_fees_lamports']:
        lines.append(f"Failed-buy fees included: {sol(report['failed_buy_fees_lamports'])} SOL")
    if report['test_closed'] or report['test_pnl_lamports']:
        lines.append(f"Tests separately: {report['test_closed']} closed; {sol(report['test_pnl_lamports'], signed=True)} SOL")
    if report['partial_day']:
        lines.append("Partial first day: detailed tracking from " + stamp(report['coverage_started_ns']) + ".")
    lines.append("Updated " + stamp(now_ns))
    return "\n".join(lines)

URGENT = {
    "EXECUTION_OUTCOME_UNKNOWN",
    "BACKGROUND_WORKER_STOPPED",
    "RUNTIME_TASK_FAILED",
    "ORDER_FAILED",
    "NOTIFICATION_DELIVERY_FAILED",
}


def format_notice(event, payload, *, ts_ns=None):
    now = datetime.fromtimestamp(ts_ns / 1e9, UK) if ts_ns else datetime.now(UK)
    urgent = event in URGENT or event == "SAFETY_MODE_CHANGED" and payload.get("mode") != "ACTIVE"
    label = "🚨" if urgent else ("💱" if event == "FILL_CONFIRMED" else "ℹ️")
    titles = {
        "FILL_CONFIRMED": "Buy confirmed" if payload.get("side") == "BUY" else "Sell confirmed",
        "ORDER_FAILED": "Trade did not complete",
        "EXECUTION_OUTCOME_UNKNOWN": "Trade still unconfirmed",
        "BACKGROUND_WORKER_STOPPED": "Bot worker stopped",
        "RUNTIME_TASK_FAILED": "Bot task needs attention",
        "SAFETY_MODE_CHANGED": "Trading status changed",
        "PIPELINE_DEGRADED": "A bot step needs attention",
        "CANDIDATE_UNAVAILABLE": "Token skipped",
        "RUNNER_STARTED": "Bot started",
    }
    if event == "FILL_CONFIRMED" and payload.get("side") == "SELL" and 'position_closed' in payload:
        titles[event] = "Trade closed" if payload.get("position_closed") else "Partial sale confirmed"
    if payload.get("test_operation"):
        label = "TEST"
    lines = [
        f"{label} Gambit · {titles.get(event, 'Bot update')}",
        now.strftime("%H:%M %Z · %d %b"),
    ]
    mint = payload.get("mint")
    if mint:
        lines.append("Token: " + str(mint)[:64])
    if payload.get("stage"):
        lines.append("Step: " + readable(payload["stage"]))
    if payload.get("reason"):
        descriptions = {
            "METADATA_HTTP_REJECTED": "Token metadata host returned an error",
            "METADATA_TIMEOUT": "Token metadata did not load in time",
            "METADATA_NETWORK_FAILED": "Could not connect to the token metadata host",
            "METADATA_JSON_INVALID": "Token metadata is invalid",
            "METADATA_NOT_OBJECT": "Token metadata has an unsupported format",
            "METADATA_TOO_LARGE": "Token metadata exceeds the size limit",
            "METADATA_GATEWAY_COOLDOWN": "Metadata services are rate-limited; waiting for their retry window",
            "METADATA_IPFS_INTEGRITY_FAILED": "Metadata did not match the token's published content hash",
            "METADATA_IPFS_LAYOUT_UNSUPPORTED": "Could not verify this metadata file safely",
            "METADATA_IPFS_CID_UNSUPPORTED": "The metadata content identifier is unsupported",
        }
        lines.append("Why: " + descriptions.get(payload["reason"], readable(payload["reason"])))
    if payload.get("http_status"):
        lines.append("Service response: " + str(payload["http_status"]))
    if payload.get("metadata_attempt"):
        lines.append("Checks: " + str(payload["metadata_attempt"]))
    if event == "EXECUTION_OUTCOME_UNKNOWN":
        lines.append(
            "The transaction may have gone through. Gambit is checking it. Do not retry or send a replacement."
        )
    elif event == "FILL_CONFIRMED":
        delta = payload.get("sol_delta_lamports")
        entry = payload.get("entry_debit_lamports")
        pnl = payload.get("trade_pnl_lamports")
        if delta is not None:
            if payload.get("side") == "BUY":
                lines += [f"Wallet spent: {sol(-delta)} SOL (including fees)",
                          "Trade P/L: pending — position open; no unrealised estimate."]
            elif payload.get("position_closed") and pnl is not None:
                outcome = "✅ Win" if pnl > 0 else "🔴 Loss" if pnl < 0 else "⚪ Break-even"
                percent = f" ({pnl/entry:+.2%})" if entry and entry > 0 else ""
                lines += [f"{outcome} · Trade P/L: {sol(pnl, signed=True)} SOL{percent}",
                          f"Sale received: {sol(delta)} SOL",
                          f"Held {payload.get('hold_ms', 0)/1000:.1f}s · Sold because: {readable(payload.get('exit_reason'))}"]
            else:
                lines.append(f"Sale received: {sol(delta)} SOL")
                if payload.get("sale_pnl_lamports") is not None:
                    lines.append(f"This part's P/L: {sol(payload['sale_pnl_lamports'], signed=True)} SOL")
                original = payload.get("original_tokens_raw")
                remaining = payload.get("remaining_tokens_raw", 0)
                if original:
                    lines.append(f"Still held: {remaining/original:.1%} · Final trade P/L pending")
            if payload.get("all_time_pnl_lamports") is not None:
                lines.append(f"All-time closed P/L: {sol(payload['all_time_pnl_lamports'], signed=True)} SOL")
            lines.append("Confirmed wallet amounts include fees.")
        signature = payload.get("signature")
        if signature:
            lines.append("Receipt: https://solscan.io/tx/" + str(signature)[:90])
        if delta is None:
            lines.append("P/L is unavailable in this older fill record.")
        if payload.get("test_operation"):
            lines.append("Test trade · not included in strategy results.")
    elif event == "SAFETY_MODE_CHANGED":
        lines.append("Mode: " + str(payload.get("mode", "unknown")).replace("_", " "))
        lines.append("Check /bot status for positions and next steps.")
    elif urgent:
        if event == "ORDER_FAILED":
            lines.append(f"Network fee: {sol(payload.get('fee_lamports', 0))} SOL")
            lines.append("No completed trade was recorded.")
        lines.append(
            "Check /bot status and /bot logs before taking action."
        )
    elif event == "CANDIDATE_UNAVAILABLE":
        lines.append("No trade was placed. Open /bot logs for details.")
    elif event == "PIPELINE_DEGRADED":
        lines.append("Check /bot status and /bot logs for details.")
    return "\n".join(lines), (1 if urgent else 0)
