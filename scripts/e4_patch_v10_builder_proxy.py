#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one match, found {count}")
    return text.replace(old, new, 1)


def main() -> int:
    proxy = Path("tools/e4-builder/race-proxy-v3.mjs")
    text = proxy.read_text(encoding="utf-8")
    text = replace_once(
        text,
        '''  function validResponse(line) {
    try {
      const value = JSON.parse(line);
      if (!value || typeof value !== "object") return false;
      return Boolean(
        value.transaction ||
        value.transaction_base64 ||
        value.serialized_transaction ||
        value.serializedTransaction ||
        value.ok === true ||
        value.success === true,
      );
    } catch {
      return false;
    }
  }
''',
        '''  function validResponse(line) {
    try {
      const value = JSON.parse(line);
      if (!value || typeof value !== "object" || Array.isArray(value)) return false;
      if (value.error || value.ok === false || value.success === false) return false;
      // Different builder versions use different transaction field names. Any
      // non-error JSON object is a valid first response; Python performs the
      // authoritative schema validation before signing.
      return true;
    } catch {
      return false;
    }
  }
''',
        "raced builder response validation",
    )
    proxy.write_text(text, encoding="utf-8")

    preload = Path("tools/e4-builder/fast-preload-v3.mjs")
    ptext = preload.read_text(encoding="utf-8")
    ptext = replace_once(
        ptext,
        '''async function prewarm(url) {
  const body = JSON.stringify({
    jsonrpc: "2.0",
    id: 1,
    method: "getLatestBlockhash",
    params: [{commitment: "processed"}],
  });
  try {
    await cachedFetch(url, {
      method: "POST",
      headers: {"content-type": "application/json"},
      body,
    });
  } catch {
    // The normal builder path owns error reporting. Warm-up must never crash it.
  }
}

for (const url of rpcUrls) void prewarm(url);
const timer = scheduleInterval(() => {
  for (const url of rpcUrls) void prewarm(url);
}, 2_000);
''',
        '''async function prewarm(url) {
  const requests = [
    [],
    [{commitment: "processed"}],
    [{commitment: "confirmed"}],
  ];
  await Promise.allSettled(
    requests.map((params, index) => cachedFetch(url, {
      method: "POST",
      headers: {"content-type": "application/json"},
      body: JSON.stringify({
        jsonrpc: "2.0",
        id: index + 1,
        method: "getLatestBlockhash",
        params,
      }),
    })),
  );
}

for (const url of rpcUrls) void prewarm(url);
const timer = scheduleInterval(() => {
  for (const url of rpcUrls) void prewarm(url);
}, 2_000);
''',
        "multi-commitment blockhash warmup",
    )
    preload.write_text(ptext, encoding="utf-8")
    print("patched raced builder validation and blockhash warmup")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
