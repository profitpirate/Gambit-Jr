import {setInterval as scheduleInterval} from "node:timers";

const nativeFetch = globalThis.fetch.bind(globalThis);
const cache = new Map();
const inFlight = new Map();
const staticAccounts = new Set(
  String(process.env.E4_FAST_STATIC_ACCOUNTS || "")
    .split(",")
    .map((value) => value.trim())
    .filter(Boolean),
);
const rpcUrls = Array.from(
  new Set(
    [
      process.env.E4_RPC_URL,
      process.env.SOLANA_RPC_URL,
      process.env.HELIUS_RPC_URL,
      ...String(process.env.E4_RPC_URLS || "").split(","),
    ]
      .map((value) => String(value || "").trim())
      .filter(Boolean),
  ),
);

function bodyText(input, init) {
  if (typeof init?.body === "string") return init.body;
  if (init?.body instanceof Uint8Array) return Buffer.from(init.body).toString("utf8");
  if (input instanceof Request && !init?.body) return null;
  return null;
}

function rpcCachePolicy(body) {
  if (!body) return null;
  let payload;
  try {
    payload = JSON.parse(body);
  } catch {
    return null;
  }
  if (!payload || Array.isArray(payload) || typeof payload !== "object") return null;
  const method = String(payload.method || "");
  const params = Array.isArray(payload.params) ? payload.params : [];
  if (method === "getLatestBlockhash") {
    return {key: `rpc:${method}:${JSON.stringify(params)}`, ttlMs: 12_000, staleMs: 20_000};
  }
  if (method === "getRecentPrioritizationFees") {
    return {key: `rpc:${method}:${JSON.stringify(params)}`, ttlMs: 350, staleMs: 1_000};
  }
  if (method === "getAccountInfo") {
    const address = String(params[0] || "");
    if (staticAccounts.has(address)) {
      return {key: `rpc:${method}:${address}:${JSON.stringify(params[1] || {})}`, ttlMs: 300_000, staleMs: 600_000};
    }
  }
  return null;
}

function genericCachePolicy(url, method) {
  const value = String(url);
  if (method !== "GET") return null;
  if (/fee[-_/]?recipient|global|config|lookup/i.test(value)) {
    return {key: `http:${value}`, ttlMs: 60_000, staleMs: 300_000};
  }
  return null;
}

function responseFrom(entry) {
  return new Response(entry.body.slice(0), {
    status: entry.status,
    statusText: entry.statusText,
    headers: entry.headers,
  });
}

async function refresh(input, init, policy) {
  const response = await nativeFetch(input, init);
  const body = new Uint8Array(await response.arrayBuffer());
  const entry = {
    status: response.status,
    statusText: response.statusText,
    headers: Array.from(response.headers.entries()),
    body,
    storedAt: Date.now(),
    ttlMs: policy.ttlMs,
    staleMs: policy.staleMs,
  };
  if (response.ok) cache.set(policy.key, entry);
  return responseFrom(entry);
}

async function cachedFetch(input, init = undefined) {
  const method = String(init?.method || (input instanceof Request ? input.method : "GET")).toUpperCase();
  const url = input instanceof Request ? input.url : String(input);
  const body = bodyText(input, init);
  const policy = rpcCachePolicy(body) || genericCachePolicy(url, method);
  if (!policy) return nativeFetch(input, init);

  const now = Date.now();
  const existing = cache.get(policy.key);
  if (existing && now - existing.storedAt <= existing.ttlMs) return responseFrom(existing);
  if (existing && now - existing.storedAt <= existing.staleMs) {
    if (!inFlight.has(policy.key)) {
      const background = refresh(input, init, policy)
        .catch(() => null)
        .finally(() => inFlight.delete(policy.key));
      inFlight.set(policy.key, background);
    }
    return responseFrom(existing);
  }
  if (!inFlight.has(policy.key)) {
    const request = refresh(input, init, policy).finally(() => inFlight.delete(policy.key));
    inFlight.set(policy.key, request);
  }
  return inFlight.get(policy.key);
}

globalThis.fetch = cachedFetch;

async function prewarm(url) {
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
timer.unref();
