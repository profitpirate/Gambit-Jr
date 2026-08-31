import {Connection, PublicKey} from "@solana/web3.js";
import {setInterval as scheduleInterval} from "node:timers";

const nativeFetch = globalThis.fetch.bind(globalThis);
const responseCache = new Map();
const responseInFlight = new Map();
const lookupCache = new Map();
const lookupInFlight = new Map();
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
const warmUrl = rpcUrls[0] || "";
const warmConnection = warmUrl ? new Connection(warmUrl, "processed") : null;
let blockhashCache = null;
let blockhashRefresh = null;
let priorityFeeCache = {storedAt: 0, value: []};

function cloneResponse(entry, requestId = null) {
  let body = entry.body;
  if (requestId !== null && entry.isJsonRpc) {
    try {
      const value = JSON.parse(Buffer.from(entry.body).toString("utf8"));
      if (value && typeof value === "object" && !Array.isArray(value)) {
        value.id = requestId;
        body = new Uint8Array(Buffer.from(JSON.stringify(value)));
      }
    } catch {
      // Keep the original body; callers still receive an exact valid response.
    }
  }
  return new Response(body.slice(0), {
    status: entry.status,
    statusText: entry.statusText,
    headers: entry.headers,
  });
}

function requestBody(input, init) {
  if (typeof init?.body === "string") return init.body;
  if (init?.body instanceof Uint8Array) return Buffer.from(init.body).toString("utf8");
  return null;
}

function policy(input, init) {
  const url = input instanceof Request ? input.url : String(input);
  const method = String(init?.method || (input instanceof Request ? input.method : "GET")).toUpperCase();
  const body = requestBody(input, init);
  if (body) {
    try {
      const payload = JSON.parse(body);
      if (payload && !Array.isArray(payload) && typeof payload === "object") {
        const rpcMethod = String(payload.method || "");
        const params = Array.isArray(payload.params) ? payload.params : [];
        if (rpcMethod === "getLatestBlockhash") {
          return {
            key: `rpc:${url}:${rpcMethod}:${JSON.stringify(params)}`,
            ttlMs: 10_000,
            staleMs: 25_000,
            requestId: payload.id ?? null,
            isJsonRpc: true,
          };
        }
        if (rpcMethod === "getRecentPrioritizationFees") {
          return {
            key: `rpc:${url}:${rpcMethod}:${JSON.stringify(params)}`,
            ttlMs: 300,
            staleMs: 1_000,
            requestId: payload.id ?? null,
            isJsonRpc: true,
          };
        }
      }
    } catch {
      // Not JSON-RPC.
    }
  }
  if (method === "GET" && /fee[-_/]?recipient|global|config|lookup/i.test(url)) {
    return {key: `http:${url}`, ttlMs: 60_000, staleMs: 300_000, requestId: null, isJsonRpc: false};
  }
  return null;
}

async function fetchAndStore(input, init, selected) {
  const response = await nativeFetch(input, init);
  const body = new Uint8Array(await response.arrayBuffer());
  const entry = {
    status: response.status,
    statusText: response.statusText,
    headers: Array.from(response.headers.entries()),
    body,
    storedAt: Date.now(),
    ttlMs: selected.ttlMs,
    staleMs: selected.staleMs,
    isJsonRpc: selected.isJsonRpc,
  };
  if (response.ok) responseCache.set(selected.key, entry);
  return cloneResponse(entry, selected.requestId);
}

async function cachedFetch(input, init = undefined) {
  const selected = policy(input, init);
  if (!selected) return nativeFetch(input, init);
  const now = Date.now();
  const existing = responseCache.get(selected.key);
  if (existing && now - existing.storedAt <= existing.ttlMs) {
    return cloneResponse(existing, selected.requestId);
  }
  if (existing && now - existing.storedAt <= existing.staleMs) {
    if (!responseInFlight.has(selected.key)) {
      const refresh = fetchAndStore(input, init, selected)
        .catch(() => null)
        .finally(() => responseInFlight.delete(selected.key));
      responseInFlight.set(selected.key, refresh);
    }
    return cloneResponse(existing, selected.requestId);
  }
  if (!responseInFlight.has(selected.key)) {
    const request = fetchAndStore(input, init, selected)
      .finally(() => responseInFlight.delete(selected.key));
    responseInFlight.set(selected.key, request);
  }
  return responseInFlight.get(selected.key);
}

globalThis.fetch = cachedFetch;

const originalLatestBlockhash = Connection.prototype.getLatestBlockhash;
const originalLatestBlockhashContext = Connection.prototype.getLatestBlockhashAndContext;
const originalLookup = Connection.prototype.getAddressLookupTable;
const originalPriorityFees = Connection.prototype.getRecentPrioritizationFees;

async function refreshBlockhash() {
  if (!warmConnection) return null;
  if (blockhashRefresh) return blockhashRefresh;
  blockhashRefresh = originalLatestBlockhashContext
    .call(warmConnection, "processed")
    .then((value) => {
      blockhashCache = {storedAt: Date.now(), context: value};
      return blockhashCache;
    })
    .catch(() => blockhashCache)
    .finally(() => {
      blockhashRefresh = null;
    });
  return blockhashRefresh;
}

Connection.prototype.getLatestBlockhashAndContext = async function patchedLatestBlockhashAndContext(commitment) {
  const now = Date.now();
  if (blockhashCache && now - blockhashCache.storedAt <= 10_000) {
    return blockhashCache.context;
  }
  const refreshed = await refreshBlockhash();
  if (refreshed) return refreshed.context;
  return originalLatestBlockhashContext.call(this, commitment);
};

Connection.prototype.getLatestBlockhash = async function patchedLatestBlockhash(commitment) {
  const value = await this.getLatestBlockhashAndContext(commitment);
  return value.value;
};

Connection.prototype.getAddressLookupTable = async function patchedLookupTable(accountKey, config) {
  const key = String(accountKey);
  const cached = lookupCache.get(key);
  if (cached && Date.now() - cached.storedAt <= 300_000) return cached.value;
  if (!lookupInFlight.has(key)) {
    const request = originalLookup
      .call(this, accountKey instanceof PublicKey ? accountKey : new PublicKey(accountKey), config)
      .then((value) => {
        lookupCache.set(key, {storedAt: Date.now(), value});
        return value;
      })
      .finally(() => lookupInFlight.delete(key));
    lookupInFlight.set(key, request);
  }
  return lookupInFlight.get(key);
};

Connection.prototype.getRecentPrioritizationFees = async function patchedPriorityFees(config) {
  if (Date.now() - priorityFeeCache.storedAt <= 300) return priorityFeeCache.value;
  const value = await originalPriorityFees.call(this, config);
  priorityFeeCache = {storedAt: Date.now(), value};
  return value;
};

await refreshBlockhash();
const timer = scheduleInterval(() => {
  void refreshBlockhash();
}, 2_000);
timer.unref();
