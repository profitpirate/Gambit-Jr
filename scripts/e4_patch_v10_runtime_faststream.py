#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one match, found {count}")
    return text.replace(old, new, 1)


def main() -> int:
    path = Path("src/memecoin_bot/e4_pipeline_runtime_v10.py")
    text = path.read_text(encoding="utf-8")
    text = replace_once(
        text,
        '''        self.ws_urls = _csv_env("E4_PIPELINE_SOLANA_WS_URLS")
        self.social_stream_url = os.getenv("E4_SOCIAL_STREAM_URL", "").strip()
''',
        '''        self.ws_urls = _csv_env("E4_PIPELINE_SOLANA_WS_URLS")
        # Enhanced transaction streams deliver the full transaction in the
        # notification and avoid the slow logsSubscribe -> getTransaction hop.
        self.transaction_ws_urls = _csv_env("E4_PIPELINE_TRANSACTION_WS_URLS")
        self.social_stream_url = os.getenv("E4_SOCIAL_STREAM_URL", "").strip()
''',
        "transaction stream config",
    )
    marker = '''    async def social_stream_worker(self) -> None:
'''
    method = '''    async def e4_transaction_ws_worker(self, url: str) -> None:
        """Consume provider transactionSubscribe notifications in one hop."""
        assert self.session is not None
        while not self.stop_event.is_set():
            try:
                async with self.session.ws_connect(
                    url,
                    heartbeat=10,
                    max_msg_size=16 * 1024 * 1024,
                ) as ws:
                    await ws.send_json(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "transactionSubscribe",
                            "params": [
                                {
                                    "accountInclude": [E4_WALLET],
                                    "failed": False,
                                    "vote": False,
                                },
                                {
                                    "commitment": "processed",
                                    "encoding": "jsonParsed",
                                    "transactionDetails": "full",
                                    "showRewards": False,
                                    "maxSupportedTransactionVersion": 0,
                                },
                            ],
                        }
                    )
                    async for message in ws:
                        if self.stop_event.is_set():
                            break
                        if message.type != aiohttp.WSMsgType.TEXT:
                            continue
                        envelope = _json_mapping(message.data)
                        result = ((envelope.get("params") or {}).get("result") or {})
                        value = result.get("value") if isinstance(result, Mapping) else None
                        if not isinstance(value, Mapping):
                            continue
                        signature = str(
                            value.get("signature")
                            or ((value.get("transaction") or {}).get("signatures") or [""])[0]
                            or ""
                        )
                        tx = value.get("transaction") if isinstance(value.get("transaction"), Mapping) else value
                        if signature and isinstance(tx, Mapping):
                            # Providers differ: some put meta alongside transaction,
                            # others wrap both under value. Normalize for one parser.
                            normalized = dict(tx)
                            if "meta" not in normalized and isinstance(value.get("meta"), Mapping):
                                normalized["meta"] = value["meta"]
                            self.metrics.e4_notifications += 1
                            self.process_e4_transaction(signature, normalized)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.record_error(exc)
                try:
                    await asyncio.wait_for(self.stop_event.wait(), timeout=0.20)
                except asyncio.TimeoutError:
                    pass

'''
    text = replace_once(text, marker, method + marker, "transaction stream method")
    text = replace_once(
        text,
        '''            tasks.extend(
                asyncio.create_task(self.e4_ws_worker(url), name=f"e4-v10-wallet-{index}")
                for index, url in enumerate(self.ws_urls)
            )
            if self.social_stream_url:
''',
        '''            tasks.extend(
                asyncio.create_task(self.e4_ws_worker(url), name=f"e4-v10-wallet-{index}")
                for index, url in enumerate(self.ws_urls)
            )
            tasks.extend(
                asyncio.create_task(
                    self.e4_transaction_ws_worker(url),
                    name=f"e4-v10-transaction-stream-{index}",
                )
                for index, url in enumerate(self.transaction_ws_urls)
            )
            if self.social_stream_url:
''',
        "transaction stream tasks",
    )
    text = replace_once(
        text,
        '''        "runtime": vars(runtime.metrics) if runtime else {},
''',
        '''        "runtime": {
            "udp_messages": runtime.metrics.udp_messages,
            "social_messages": runtime.metrics.social_messages,
            "social_reconnects": runtime.metrics.social_reconnects,
            "e4_notifications": runtime.metrics.e4_notifications,
            "e4_transactions": runtime.metrics.e4_transactions,
            "e4_transaction_misses": runtime.metrics.e4_transaction_misses,
            "errors": runtime.metrics.errors,
            "last_error": runtime.metrics.last_error,
            "started_ns": runtime.metrics.started_ns,
        } if runtime else {},
''',
        "runtime slots snapshot",
    )
    path.write_text(text, encoding="utf-8")
    print("patched V10 enhanced transaction stream and runtime snapshot")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
