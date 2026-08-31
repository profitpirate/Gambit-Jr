import BN from "bn.js";
import {
  OnlinePumpSdk,
  PUMP_PROGRAM_ID,
  PUMP_SDK,
  getBuySolAmountFromTokenAmount,
  getBuyTokenAmountFromSolAmount,
  getSellSolAmountFromTokenAmount,
} from "@pump-fun/pump-sdk";
import { TOKEN_2022_PROGRAM_ID, TOKEN_PROGRAM_ID } from "@solana/spl-token";
import {
  ComputeBudgetProgram,
  Connection,
  PublicKey,
  SystemProgram,
  TransactionMessage,
  VersionedTransaction,
} from "@solana/web3.js";

const RPC_URLS = [...new Set([
  process.env.E4_PRIMARY_RPC_URL || process.env.SOLANA_RPC_URL || "https://api.mainnet-beta.solana.com",
  ...String(process.env.E4_FALLBACK_RPC_URLS || "").split(",").map((v) => v.trim()).filter(Boolean),
])];
const connections = RPC_URLS.map((url) => new Connection(url, "processed"));
const COMPUTE_UNITS = Math.max(80_000, Math.min(350_000, Number(process.env.E4_LOCAL_COMPUTE_UNITS || 220_000)));
const BLOCKHASH_REFRESH_MS = Math.max(150, Number(process.env.E4_BLOCKHASH_REFRESH_MS || 350));
const GLOBAL_REFRESH_MS = Math.max(10_000, Number(process.env.E4_GLOBAL_REFRESH_MS || 30_000));
const CURVE_TTL_MS = Math.max(500, Number(process.env.E4_CURVE_CACHE_TTL_MS || 15_000));
const MAX_WIRE_BYTES = 1232;
const JITO_TIPS = [
  "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5",
  "HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe",
  "Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY",
  "ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zt6iGPaS49",
  "DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh",
  "ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcNZ5pGwDcEt",
  "DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL",
  "3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT",
];

let globalState = null;
let feeConfig = null;
let globalAt = 0;
let blockhash = null;
let blockhashAt = 0;
let warming = null;
const curves = new Map();

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function across(fn, retries = 2) {
  let last;
  for (let attempt = 0; attempt < retries; attempt += 1) {
    for (const connection of connections) {
      try { return await fn(connection); } catch (error) { last = error; }
    }
    if (attempt + 1 < retries) await sleep(25 * (attempt + 1));
  }
  throw last || new Error("all RPC endpoints failed");
}

async function refreshBlockhash() {
  const result = await across((connection) => connection.getLatestBlockhash("processed"));
  blockhash = result.blockhash;
  blockhashAt = Date.now();
}

async function refreshGlobal() {
  globalState = await across((connection) => new OnlinePumpSdk(connection).fetchGlobal());
  try {
    feeConfig = await across((connection) => new OnlinePumpSdk(connection).fetchFeeConfig());
  } catch { feeConfig = null; }
  globalAt = Date.now();
}

export async function warm() {
  if (globalState && blockhash && Date.now() - blockhashAt < 15_000) return;
  if (!warming) {
    warming = Promise.all([
      globalState ? Promise.resolve() : refreshGlobal(),
      blockhash ? Promise.resolve() : refreshBlockhash(),
    ]).finally(() => { warming = null; });
  }
  await warming;
}

setInterval(() => refreshBlockhash().catch(() => {}), BLOCKHASH_REFRESH_MS).unref();
setInterval(() => refreshGlobal().catch(() => {}), GLOBAL_REFRESH_MS).unref();
void warm().catch(() => {});

function parseCurve(metadata) {
  const value = metadata?.curve;
  if (!value) return null;
  const required = ["virtual_token_reserves", "virtual_sol_reserves", "real_token_reserves", "real_sol_reserves", "token_total_supply", "creator"];
  if (required.some((key) => value[key] == null || value[key] === "")) return null;
  return {
    virtualTokenReserves: new BN(String(value.virtual_token_reserves), 10),
    virtualSolReserves: new BN(String(value.virtual_sol_reserves), 10),
    realTokenReserves: new BN(String(value.real_token_reserves), 10),
    realSolReserves: new BN(String(value.real_sol_reserves), 10),
    tokenTotalSupply: new BN(String(value.token_total_supply), 10),
    complete: Boolean(value.complete),
    creator: new PublicKey(String(value.creator)),
    isMayhemMode: Boolean(value.is_mayhem_mode),
    isCashbackCoin: Boolean(value.is_cashback_coin),
  };
}

export async function prefetch(request) {
  const curve = parseCurve(request.metadata);
  if (request.mint && curve) {
    curves.set(String(request.mint), {
      curve,
      tokenProgram: String(request.metadata?.token_program || ""),
      decimals: Number(request.metadata?.token_decimals ?? 6),
      updatedAt: Date.now(),
    });
  }
  await warm();
  return { prefetched: Boolean(curve), builder_mode: "local_offline_pump_sdk" };
}

function cached(request) {
  const inline = parseCurve(request.metadata);
  if (inline) {
    const value = { curve: inline, tokenProgram: String(request.metadata?.token_program || ""), decimals: Number(request.metadata?.token_decimals ?? 6), updatedAt: Date.now() };
    curves.set(String(request.mint), value);
    return value;
  }
  const value = curves.get(String(request.mint || ""));
  return value && Date.now() - value.updatedAt <= CURVE_TTL_MS ? value : null;
}

function tokenProgram(value) {
  if (value === TOKEN_PROGRAM_ID.toBase58()) return TOKEN_PROGRAM_ID;
  if (!value || value === TOKEN_2022_PROGRAM_ID.toBase58()) return TOKEN_2022_PROGRAM_ID;
  return null;
}

function tipAccount(request) {
  const seed = String(request.request_id || request.mint || request.public_key || "e4");
  let hash = 0;
  for (const character of seed) hash = ((hash * 31) + character.charCodeAt(0)) >>> 0;
  return new PublicKey(JITO_TIPS[hash % JITO_TIPS.length]);
}

function budget(request) {
  const result = [ComputeBudgetProgram.setComputeUnitLimit({ units: COMPUTE_UNITS })];
  const lamports = Math.max(0, Number(request.priority_fee_sol || 0)) * 1_000_000_000;
  if (lamports > 0) {
    result.push(ComputeBudgetProgram.setComputeUnitPrice({
      microLamports: Math.max(1, Math.floor((lamports * 1_000_000) / COMPUTE_UNITS)),
    }));
  }
  const tip = Math.max(0, Number(request.tip_sol || 0));
  if (tip > 0) {
    result.push(SystemProgram.transfer({
      fromPubkey: new PublicKey(request.public_key),
      toPubkey: tipAccount(request),
      lamports: Math.max(1_000, Math.floor(tip * 1_000_000_000)),
    }));
  }
  return result;
}

async function compile(request, sdkInstructions) {
  await warm();
  const transaction = new VersionedTransaction(new TransactionMessage({
    payerKey: new PublicKey(request.public_key),
    recentBlockhash: blockhash,
    instructions: [...budget(request), ...sdkInstructions],
  }).compileToV0Message());
  const bytes = Buffer.from(transaction.serialize());
  if (bytes.length > MAX_WIRE_BYTES) throw new Error(`local transaction exceeds wire limit: ${bytes.length}`);
  return {
    transaction_base64: bytes.toString("base64"),
    builder_mode: "local_offline_pump_sdk",
    wire_bytes: bytes.length,
    blockhash_age_ms: Date.now() - blockhashAt,
    global_age_ms: Date.now() - globalAt,
  };
}

export async function buildLocal(request) {
  const started = performance.now();
  await warm();
  const state = cached(request);
  if (!state) throw new Error("E4_LOCAL_STATE_UNAVAILABLE");
  if (state.curve.complete) throw new Error("E4_CURVE_MIGRATED");
  const program = tokenProgram(state.tokenProgram);
  if (!program) throw new Error("E4_TOKEN_PROGRAM_UNAVAILABLE");
  const mint = new PublicKey(request.mint);
  const user = new PublicKey(request.public_key);
  const accountInfo = { data: Buffer.alloc(151), executable: false, lamports: 0, owner: PUMP_PROGRAM_ID, rentEpoch: 0 };
  let sdkInstructions;
  if (String(request.side).toUpperCase() === "BUY") {
    const input = new BN(String(Math.max(1, Math.floor(Number(request.amount) * 1_000_000_000))), 10);
    const amount = getBuyTokenAmountFromSolAmount({ global: globalState, feeConfig, mintSupply: state.curve.tokenTotalSupply, bondingCurve: state.curve, amount: input });
    const solAmount = getBuySolAmountFromTokenAmount({ global: globalState, feeConfig, mintSupply: state.curve.tokenTotalSupply, bondingCurve: state.curve, amount });
    sdkInstructions = await PUMP_SDK.buyInstructions({
      global: globalState, bondingCurveAccountInfo: accountInfo, bondingCurve: state.curve,
      associatedUserAccountInfo: null, mint, user, amount, solAmount,
      slippage: Number(request.slippage_bps || 0) / 100, tokenProgram: program,
    });
  } else if (String(request.side).toUpperCase() === "SELL") {
    const scale = 10 ** (Number.isInteger(state.decimals) ? state.decimals : 6);
    const raw = Math.floor(Number(request.amount) * scale);
    if (!Number.isSafeInteger(raw) || raw <= 0) throw new Error("invalid local sell amount");
    const amount = new BN(String(raw), 10);
    const solAmount = getSellSolAmountFromTokenAmount({ global: globalState, feeConfig, mintSupply: state.curve.tokenTotalSupply, bondingCurve: state.curve, amount });
    sdkInstructions = await PUMP_SDK.sellInstructions({
      global: globalState, bondingCurveAccountInfo: accountInfo, bondingCurve: state.curve,
      mint, user, amount, solAmount, slippage: Number(request.slippage_bps || 0) / 100,
      tokenProgram: program, mayhemMode: state.curve.isMayhemMode ?? false,
      cashback: state.curve.isCashbackCoin ?? false,
    });
  } else {
    throw new Error(`unsupported local side ${request.side}`);
  }
  return { ...(await compile(request, sdkInstructions)), build_ms: performance.now() - started };
}
