#!/usr/bin/env node
import readline from "node:readline";
import BN from "bn.js";
import {
  PUMP_SDK,
  OnlinePumpSdk,
  PUMP_PROGRAM_ID,
  bondingCurvePda,
  getBuySolAmountFromTokenAmount,
  getBuyTokenAmountFromSolAmount,
  getSellSolAmountFromTokenAmount,
} from "@pump-fun/pump-sdk";
import {
  OnlinePumpAmmSdk,
  PUMP_AMM_SDK,
  canonicalPumpPoolPda,
} from "@pump-fun/pump-swap-sdk";
import {
  TOKEN_2022_PROGRAM_ID,
  TOKEN_PROGRAM_ID,
} from "@solana/spl-token";
import {
  ComputeBudgetProgram,
  Connection,
  PublicKey,
  SystemProgram,
  TransactionMessage,
  VersionedTransaction,
} from "@solana/web3.js";

const PRIMARY_RPC_URL = process.env.E4_PRIMARY_RPC_URL || process.env.SOLANA_RPC_URL || "https://api.mainnet-beta.solana.com";
const FALLBACK_RPC_URLS = String(process.env.E4_FALLBACK_RPC_URLS || "")
  .split(",")
  .map((value) => value.trim())
  .filter(Boolean);
const RPC_URLS = [...new Set([PRIMARY_RPC_URL, ...FALLBACK_RPC_URLS])];
const connections = RPC_URLS.map((url) => new Connection(url, {commitment: "processed", disableRetryOnRateLimit: true}));
const connection = connections[0];
const onlinePump = new OnlinePumpSdk(connection);
const onlineAmm = new OnlinePumpAmmSdk(connection);

const LOCAL_ENABLED = String(process.env.E4_LOCAL_PUMP_BUILDER || "true").toLowerCase() !== "false";
const ALLOW_REMOTE_FALLBACK = String(process.env.E4_ALLOW_REMOTEBUILDER_FALLBACK || "false").toLowerCase() === "true";
const TRADE_LOCAL_URL = process.env.E4_PUMPPORTAL_TRADE_LOCAL_URL || "https://pumpportal.fun/api/trade-local";
const BUILD_TIMEOUT_MS = Number(process.env.E4_BUILDER_TIMEOUT_MS || 2200);
const BUILD_RETRIES = Math.max(1, Math.min(4, Number(process.env.E4_BUILDER_RETRIES || 3)));
const STATE_CACHE_TTL_MS = Math.max(5, Number(process.env.E4_BUILDER_STATE_TTL_MS || 250));
const GLOBAL_CACHE_TTL_MS = Math.max(1_000, Number(process.env.E4_BUILDER_GLOBAL_TTL_MS || 30_000));
const BLOCKHASH_CACHE_TTL_MS = Math.max(250, Number(process.env.E4_BUILDER_BLOCKHASH_TTL_MS || 8_000));
const BUY_SELL_COMPUTE_UNITS = Math.max(80_000, Number(process.env.E4_BUILDER_BONDING_COMPUTE_UNITS || 120_000));
const AMM_COMPUTE_UNITS = Math.max(120_000, Number(process.env.E4_BUILDER_AMM_COMPUTE_UNITS || 200_000));
const MAX_WIRE_BYTES = 1232;
const TOKEN_DECIMALS = Math.max(0, Number(process.env.E4_PUMP_TOKEN_DECIMALS || 6));
const DEFAULT_TOKEN_PROGRAM = String(process.env.E4_DEFAULT_TOKEN_PROGRAM || "token2022").toLowerCase();

const JITO_TIP_ACCOUNTS = [
  "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5",
  "HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe",
  "Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY",
  "ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zt6iGPaS49",
  "DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh",
  "ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcNZ5pGwDcEt",
  "DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL",
  "3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT",
];

const sleep = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));
const stateCache = new Map();
const stateInflight = new Map();
let globalCache = null;
let globalInflight = null;
let blockhashCache = null;
let blockhashInflight = null;

const metrics = {
  local_builds: 0,
  remote_fallback_builds: 0,
  prefetches: 0,
  cache_hits: 0,
  cache_misses: 0,
  failures: 0,
};

function deterministicTipAccount(request) {
  const seed = String(request.request_id || request.mint || request.public_key || "e4");
  let hash = 0;
  for (const character of seed) hash = ((hash * 31) + character.charCodeAt(0)) >>> 0;
  return new PublicKey(JITO_TIP_ACCOUNTS[hash % JITO_TIP_ACCOUNTS.length]);
}

function boolValue(value, fallback = false) {
  if (value == null) return fallback;
  if (typeof value === "boolean") return value;
  return ["1", "true", "yes", "on"].includes(String(value).toLowerCase());
}

function finiteNumber(value, fallback = null) {
  const number = Number(value);
  return Number.isFinite(number) ? number : fallback;
}

function bn(value, name) {
  if (value == null || value === "") throw new Error(`${name} is required`);
  const text = typeof value === "number" ? Math.trunc(value).toString() : String(value);
  if (!/^\d+$/.test(text)) throw new Error(`${name} must be a non-negative integer`);
  return new BN(text, 10);
}

function solToLamports(value) {
  const number = finiteNumber(value);
  if (number == null || number <= 0) throw new Error("SOL amount must be positive");
  return new BN(Math.round(number * 1_000_000_000).toString(), 10);
}

function uiTokensToRaw(value) {
  const number = finiteNumber(value);
  if (number == null || number <= 0) throw new Error("token amount must be positive");
  const raw = Math.round(number * 10 ** TOKEN_DECIMALS);
  if (!Number.isSafeInteger(raw)) {
    const [whole, fraction = ""] = String(value).split(".");
    return new BN(`${whole}${fraction.padEnd(TOKEN_DECIMALS, "0").slice(0, TOKEN_DECIMALS)}`, 10);
  }
  return new BN(String(raw), 10);
}

function tokenProgramFromValue(value) {
  if (!value) {
    return DEFAULT_TOKEN_PROGRAM === "legacy" ? TOKEN_PROGRAM_ID : TOKEN_2022_PROGRAM_ID;
  }
  const normalized = String(value).toLowerCase();
  if (["token2022", "token-2022", TOKEN_2022_PROGRAM_ID.toBase58().toLowerCase()].includes(normalized)) {
    return TOKEN_2022_PROGRAM_ID;
  }
  if (["legacy", "spl-token", "token", TOKEN_PROGRAM_ID.toBase58().toLowerCase()].includes(normalized)) {
    return TOKEN_PROGRAM_ID;
  }
  return new PublicKey(String(value));
}

function cacheKey(request) {
  return `${request.mint}:${request.public_key}`;
}

function stateHint(request) {
  const value = request?.metadata?.state_hint;
  return value && typeof value === "object" ? value : null;
}

function hintBondingState(request) {
  const hint = stateHint(request);
  if (!hint) return null;
  const required = [
    "virtual_token_reserves",
    "virtual_sol_reserves",
    "real_token_reserves",
    "real_sol_reserves",
    "token_total_supply",
    "creator",
  ];
  if (!required.every((key) => hint[key] != null && hint[key] !== "")) return null;
  const mint = new PublicKey(request.mint);
  const user = new PublicKey(request.public_key);
  const tokenProgram = tokenProgramFromValue(hint.token_program);
  return {
    mint,
    user,
    tokenProgram,
    bondingCurveAddress: bondingCurvePda(mint),
    bondingCurveAccountInfo: {
      executable: false,
      lamports: 0,
      owner: PUMP_PROGRAM_ID,
      rentEpoch: 0,
      data: Buffer.alloc(151),
    },
    bondingCurve: {
      virtualTokenReserves: bn(hint.virtual_token_reserves, "virtual_token_reserves"),
      virtualSolReserves: bn(hint.virtual_sol_reserves, "virtual_sol_reserves"),
      realTokenReserves: bn(hint.real_token_reserves, "real_token_reserves"),
      realSolReserves: bn(hint.real_sol_reserves, "real_sol_reserves"),
      tokenTotalSupply: bn(hint.token_total_supply, "token_total_supply"),
      complete: boolValue(hint.complete),
      creator: new PublicKey(hint.creator),
      isMayhemMode: boolValue(hint.mayhem_mode),
      isCashbackCoin: boolValue(hint.cashback),
    },
    associatedUserAccountInfo: boolValue(hint.user_ata_exists) ? {
      executable: false,
      lamports: 0,
      owner: tokenProgram,
      rentEpoch: 0,
      data: Buffer.alloc(1),
    } : null,
    fetchedAt: Date.now(),
    source: "event_hint",
  };
}

async function fetchGlobalState(force = false) {
  const now = Date.now();
  if (!force && globalCache && now - globalCache.fetchedAt <= GLOBAL_CACHE_TTL_MS) return globalCache;
  if (globalInflight) return globalInflight;
  globalInflight = (async () => {
    let lastError;
    for (const candidate of connections) {
      try {
        const online = new OnlinePumpSdk(candidate);
        const [global, feeConfig] = await Promise.all([online.fetchGlobal(), online.fetchFeeConfig()]);
        globalCache = {global, feeConfig, fetchedAt: Date.now()};
        return globalCache;
      } catch (error) {
        lastError = error;
      }
    }
    throw lastError || new Error("all RPCs failed to fetch Pump global state");
  })();
  try {
    return await globalInflight;
  } finally {
    globalInflight = null;
  }
}

async function latestBlockhash(force = false) {
  const now = Date.now();
  if (!force && blockhashCache && now - blockhashCache.fetchedAt <= BLOCKHASH_CACHE_TTL_MS) return blockhashCache;
  if (blockhashInflight) return blockhashInflight;
  blockhashInflight = (async () => {
    let lastError;
    for (const candidate of connections) {
      try {
        const value = await candidate.getLatestBlockhash("processed");
        blockhashCache = {...value, fetchedAt: Date.now()};
        return blockhashCache;
      } catch (error) {
        lastError = error;
      }
    }
    throw lastError || new Error("all RPCs failed to return a blockhash");
  })();
  try {
    return await blockhashInflight;
  } finally {
    blockhashInflight = null;
  }
}

async function fetchBondingState(request, force = false) {
  const hinted = hintBondingState(request);
  if (hinted) return hinted;
  const key = cacheKey(request);
  const cached = stateCache.get(key);
  if (!force && cached && Date.now() - cached.fetchedAt <= STATE_CACHE_TTL_MS) {
    metrics.cache_hits += 1;
    return cached;
  }
  if (stateInflight.has(key)) return stateInflight.get(key);
  metrics.cache_misses += 1;
  const promise = (async () => {
    const mint = new PublicKey(request.mint);
    const user = new PublicKey(request.public_key);
    const hint = stateHint(request);
    let tokenProgram = hint?.token_program ? tokenProgramFromValue(hint.token_program) : null;
    let lastError;
    for (let attempt = 0; attempt < BUILD_RETRIES; attempt += 1) {
      for (const candidate of connections) {
        try {
          const online = new OnlinePumpSdk(candidate);
          if (!tokenProgram) {
            const mintInfo = await candidate.getAccountInfo(mint, "processed");
            if (!mintInfo) throw new Error(`mint account unavailable: ${request.mint}`);
            tokenProgram = mintInfo.owner.equals(TOKEN_2022_PROGRAM_ID) ? TOKEN_2022_PROGRAM_ID : TOKEN_PROGRAM_ID;
          }
          const state = await online.fetchBuyState(mint, user, tokenProgram);
          const value = {mint, user, ...state, fetchedAt: Date.now(), source: "rpc_cache"};
          stateCache.set(key, value);
          return value;
        } catch (error) {
          lastError = error;
        }
      }
      await sleep(Math.min(80, 5 * 2 ** attempt));
    }
    throw lastError || new Error(`unable to fetch Pump state for ${request.mint}`);
  })();
  stateInflight.set(key, promise);
  try {
    return await promise;
  } finally {
    stateInflight.delete(key);
  }
}

async function prefetch(request) {
  metrics.prefetches += 1;
  await Promise.all([fetchGlobalState(), latestBlockhash(), fetchBondingState(request, false)]);
  return {prefetched: true, mint: request.mint, source: stateCache.get(cacheKey(request))?.source || "event_hint"};
}

function computeBudgetInstructions(units, priorityFeeSol) {
  const fee = Math.max(0, finiteNumber(priorityFeeSol, 0));
  const microLamports = fee > 0 ? Math.max(1, Math.floor((fee * 1e15) / units)) : 0;
  return [
    ComputeBudgetProgram.setComputeUnitLimit({units}),
    ComputeBudgetProgram.setComputeUnitPrice({microLamports}),
  ];
}

function tipInstruction(request) {
  const requestedSol = Math.max(0, finiteNumber(request.tip_sol, 0));
  if (requestedSol <= 0) return [];
  const lamports = Math.max(1_000, Math.floor(requestedSol * 1_000_000_000));
  return [SystemProgram.transfer({
    fromPubkey: new PublicKey(request.public_key),
    toPubkey: deterministicTipAccount(request),
    lamports,
  })];
}

async function compileTransaction(request, instructions, units) {
  const payerKey = new PublicKey(request.public_key);
  const {blockhash} = await latestBlockhash();
  const message = new TransactionMessage({
    payerKey,
    recentBlockhash: blockhash,
    instructions: [
      ...computeBudgetInstructions(units, request.priority_fee_sol),
      ...tipInstruction(request),
      ...instructions,
    ],
  }).compileToV0Message();
  const transaction = new VersionedTransaction(message);
  const bytes = Buffer.from(transaction.serialize());
  if (bytes.length > MAX_WIRE_BYTES) throw new Error(`transaction exceeds Solana wire limit: ${bytes.length}`);
  return {
    transaction_base64: bytes.toString("base64"),
    tip_appended: tipInstruction(request).length > 0,
    tip_account: tipInstruction(request).length > 0 ? deterministicTipAccount(request).toBase58() : null,
  };
}

async function buildBonding(request) {
  const [protocol, state] = await Promise.all([fetchGlobalState(), fetchBondingState(request)]);
  if (state.bondingCurve.complete) throw new Error("bonding curve complete; AMM route required");
  const slippage = Math.max(0, Number(request.slippage_bps || 0) / 100);
  let instructions;
  let quote;
  if (String(request.side).toUpperCase() === "BUY") {
    const inputSolAmount = solToLamports(request.amount);
    const tokenAmount = getBuyTokenAmountFromSolAmount({
      global: protocol.global,
      feeConfig: protocol.feeConfig,
      mintSupply: state.bondingCurve.tokenTotalSupply,
      bondingCurve: state.bondingCurve,
      amount: inputSolAmount,
    });
    const solAmount = getBuySolAmountFromTokenAmount({
      global: protocol.global,
      feeConfig: protocol.feeConfig,
      mintSupply: state.bondingCurve.tokenTotalSupply,
      bondingCurve: state.bondingCurve,
      amount: tokenAmount,
    });
    instructions = await PUMP_SDK.buyInstructions({
      global: protocol.global,
      bondingCurveAccountInfo: state.bondingCurveAccountInfo,
      bondingCurve: state.bondingCurve,
      associatedUserAccountInfo: state.associatedUserAccountInfo ?? null,
      mint: state.mint,
      user: state.user,
      amount: tokenAmount,
      solAmount,
      slippage,
      tokenProgram: state.tokenProgram,
    });
    quote = {token_amount: tokenAmount.toString(), sol_lamports: solAmount.toString()};
  } else {
    const tokenAmount = uiTokensToRaw(request.amount);
    const solAmount = getSellSolAmountFromTokenAmount({
      global: protocol.global,
      feeConfig: protocol.feeConfig,
      mintSupply: state.bondingCurve.tokenTotalSupply,
      bondingCurve: state.bondingCurve,
      amount: tokenAmount,
    });
    instructions = await PUMP_SDK.sellInstructions({
      global: protocol.global,
      bondingCurveAccountInfo: state.bondingCurveAccountInfo,
      bondingCurve: state.bondingCurve,
      mint: state.mint,
      user: state.user,
      amount: tokenAmount,
      solAmount,
      slippage,
      tokenProgram: state.tokenProgram,
      cashback: state.bondingCurve.isCashbackCoin ?? false,
    });
    quote = {token_amount: tokenAmount.toString(), sol_lamports: solAmount.toString()};
  }
  const built = await compileTransaction(request, instructions, BUY_SELL_COMPUTE_UNITS);
  return {...built, builder_mode: "official_sdk_bonding", state_source: state.source, quote};
}

async function buildAmm(request) {
  const mint = new PublicKey(request.mint);
  const user = new PublicKey(request.public_key);
  const pool = request?.metadata?.pool ? new PublicKey(request.metadata.pool) : canonicalPumpPoolPda(mint);
  const state = await onlineAmm.swapSolanaState(pool, user);
  const slippage = Math.max(0, Number(request.slippage_bps || 0) / 100);
  let instructions;
  if (String(request.side).toUpperCase() === "BUY") {
    instructions = await PUMP_AMM_SDK.buyQuoteInput(state, solToLamports(request.amount), slippage);
  } else {
    instructions = await PUMP_AMM_SDK.sellBaseInput(state, uiTokensToRaw(request.amount), slippage);
  }
  const built = await compileTransaction(request, instructions, AMM_COMPUTE_UNITS);
  return {...built, builder_mode: "official_sdk_amm", pool: pool.toBase58()};
}

async function fetchWithRetry(url, options) {
  let lastError;
  for (let attempt = 0; attempt < BUILD_RETRIES; attempt += 1) {
    try {
      const response = await fetch(url, {...options, signal: AbortSignal.timeout(BUILD_TIMEOUT_MS)});
      if (response.status === 429 || response.status >= 500) {
        const text = await response.text();
        throw new Error(`trade-local HTTP ${response.status}: ${text.slice(0, 500)}`);
      }
      return response;
    } catch (error) {
      lastError = error;
      if (attempt + 1 < BUILD_RETRIES) await sleep(Math.min(800, 75 * 2 ** attempt));
    }
  }
  throw lastError || new Error("trade-local request exhausted retries");
}

async function remoteBuild(request) {
  const payload = {
    publicKey: request.public_key,
    action: String(request.side).toLowerCase(),
    mint: request.mint,
    amount: Number(request.amount),
    denominatedInSol: request.denominated_in_sol ? "true" : "false",
    slippage: Number(request.slippage_bps) / 100,
    priorityFee: Number(request.priority_fee_sol || 0),
    pool: request.pool || "pump",
  };
  const response = await fetchWithRetry(TRADE_LOCAL_URL, {
    method: "POST",
    headers: {"content-type": "application/json"},
    body: JSON.stringify(payload),
  });
  if (!response.ok) throw new Error(`trade-local HTTP ${response.status}: ${(await response.text()).slice(0, 500)}`);
  const bytes = Buffer.from(await response.arrayBuffer());
  if (!bytes.length) throw new Error("trade-local returned an empty transaction");
  metrics.remote_fallback_builds += 1;
  return {transaction_base64: bytes.toString("base64"), builder_mode: "remote_fallback"};
}

async function buildSweep(request) {
  const destination = request.metadata?.destination;
  if (!destination) throw new Error("SWEEP request requires metadata.destination");
  const from = new PublicKey(request.public_key);
  const to = new PublicKey(destination);
  const lamports = Math.floor(Number(request.amount) * 1_000_000_000);
  if (!Number.isSafeInteger(lamports) || lamports <= 0) throw new Error("invalid sweep amount");
  return compileTransaction(request, [SystemProgram.transfer({fromPubkey: from, toPubkey: to, lamports})], 20_000);
}

async function buildTrade(request) {
  const pool = String(request.pool || "pump").toLowerCase();
  if (!LOCAL_ENABLED) return remoteBuild(request);
  try {
    const result = pool === "pump-amm" || pool === "amm" ? await buildAmm(request) : await buildBonding(request);
    metrics.local_builds += 1;
    return result;
  } catch (error) {
    if (!ALLOW_REMOTE_FALLBACK) throw error;
    return {...(await remoteBuild(request)), local_error: error?.message || String(error)};
  }
}

async function handle(line) {
  const request = JSON.parse(line);
  const action = String(request.action || request.side || "").toUpperCase();
  const started = performance.now();
  let built;
  if (action === "PING") {
    built = {ready: true, local_enabled: LOCAL_ENABLED, remote_fallback: ALLOW_REMOTE_FALLBACK, metrics};
  } else if (action === "PREFETCH") {
    built = await prefetch(request);
  } else if (action === "INVALIDATE") {
    stateCache.delete(cacheKey(request));
    built = {invalidated: true, mint: request.mint};
  } else if (action === "SWEEP") {
    built = await buildSweep(request);
  } else if (action === "BUY" || action === "SELL") {
    built = await buildTrade(request);
  } else {
    throw new Error(`unsupported builder action: ${action || "missing"}`);
  }
  return {request_id: request.request_id || null, ...built, build_ms: performance.now() - started};
}

// Warm immutable/slow protocol state and keep the blockhash off the order path.
Promise.allSettled([fetchGlobalState(), latestBlockhash()]);
setInterval(() => void latestBlockhash(true).catch(() => {}), Math.max(1_000, BLOCKHASH_CACHE_TTL_MS / 2)).unref();
setInterval(() => void fetchGlobalState(true).catch(() => {}), Math.max(5_000, GLOBAL_CACHE_TTL_MS / 2)).unref();

const input = readline.createInterface({input: process.stdin, crlfDelay: Infinity});
for await (const raw of input) {
  const line = raw.trim();
  if (!line) continue;
  try {
    process.stdout.write(`${JSON.stringify(await handle(line))}\n`);
  } catch (error) {
    metrics.failures += 1;
    let request_id = null;
    try { request_id = JSON.parse(line).request_id || null; } catch {}
    process.stdout.write(`${JSON.stringify({request_id, error: error?.stack || String(error)})}\n`);
  }
}
