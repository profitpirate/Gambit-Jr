#!/usr/bin/env node
import readline from "node:readline";
import BN from "bn.js";
import {PUMP_SDK} from "@pump-fun/pump-sdk";
import {NATIVE_MINT, TOKEN_2022_PROGRAM_ID, TOKEN_PROGRAM_ID} from "@solana/spl-token";
import {
  ComputeBudgetProgram,
  Connection,
  PublicKey,
  SystemProgram,
  TransactionMessage,
  VersionedTransaction,
} from "@solana/web3.js";

const PRIMARY_RPC_URL = process.env.E4_PRIMARY_RPC_URL || process.env.SOLANA_RPC_URL || "https://api.mainnet-beta.solana.com";
const FALLBACK_RPC_URLS = String(process.env.E4_FALLBACK_RPC_URLS || "").split(",").map((value) => value.trim()).filter(Boolean);
const connections = [...new Set([PRIMARY_RPC_URL, ...FALLBACK_RPC_URLS])].map((url) => new Connection(url, "processed"));
const TRADE_LOCAL_URL = process.env.E4_PUMPPORTAL_TRADE_LOCAL_URL || "https://pumpportal.fun/api/trade-local";
const BUILD_TIMEOUT_MS = Number(process.env.E4_BUILDER_TIMEOUT_MS || 2200);
const BUILD_RETRIES = Math.max(1, Math.min(4, Number(process.env.E4_BUILDER_RETRIES || 3)));
const RPC_RETRIES = Math.max(1, Math.min(4, Number(process.env.E4_ALT_RPC_RETRIES || 3)));
const MAX_WIRE_BYTES = 1232;
const COMPUTE_UNITS = Math.max(150_000, Number(process.env.E4_LOCAL_COMPUTE_UNITS || 360_000));
const LOCAL_ENABLED = String(process.env.E4_LOCAL_PUMP_BUILDER || "true").toLowerCase() !== "false";
const REMOTE_FALLBACK = String(process.env.E4_REMOTE_BUILDER_FALLBACK || "true").toLowerCase() !== "false";
const TOKEN_DECIMALS = BigInt(Math.max(0, Number(process.env.E4_PUMP_TOKEN_DECIMALS || 6)));

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
const NORMAL_FEE_RECIPIENTS = [
  "62qc2CNXwrYqQScmEdiZFFAnJR262PxWEuNQtxfafNgV",
  "7VtfL8fvgNfhz17qKRMjzQEXgbdpnHHHQRh54R9jP2RJ",
  "7hTckgnGnLQR6sdH7YkqFTAA7VwTfYFaZ6EhEsU3saCX",
  "9rPYyANsfQZw3DnDmKE3YCQF5E8oD89UXoHn9JFEhJUz",
  "AVmoTthdrX6tKt4nDjco2D775W2YK3sDhxPcMmzUAmTY",
  "CebN5WGQ4jvEPvsVU4EoHEpgzq1VV7AbicfhtW4xC9iM",
  "FWsW1xNtWscwNmKv6wVsU1iTzRN6wmmk3MjxRP5tT7hz",
  "G5UZAVbAf46s7cKWoyKu8kYTip9DGTpbLZ2qa9Aq69dP",
];
const MAYHEM_FEE_RECIPIENTS = [
  "GesfTA3X2arioaHp8bbKdjG9vJtskViWACZoYvxp4twS",
  "4budycTjhs9fD6xw62VBducVTNgMgJJ5BgtKq7mAZwn6",
  "8SBKzEQU4nLSzcwF4a74F2iaUDQyTfjGndn6qUWBnrpR",
  "4UQeTP1T39KZ9Sfxzo3WR5skgsaP6NZa87BAkuazLEKH",
  "8sNeir4QsLsJdYpc9RZacohhK1Y5FLU3nC5LXgYB4aa6",
  "Fh9HmeLNUMVCvejxCtCL2DbYaRyBFVJ5xrWkLnMH6fdk",
  "463MEnMeGyJekNZFQSTUABBEbLnvMTALbT6ZmsxAbAdq",
  "6AUH3WEHucYZyC61hqpqYUWVto5qA5hjHuNQ32GNnNxA",
];
const BUYBACK_FEE_RECIPIENTS = [
  "5YxQFdt3Tr9zJLvkFccqXVUwhdTWJQc1fFg2YPbxvxeD",
  "9M4giFFMxmFGXtc3feFzRai56WbBqehoSeRE5GK7gf7",
  "GXPFM2caqTtQYC2cJ5yJRi9VDkpsYZXzYdwYpGnLmtDL",
  "3BpXnfJaUTiwXnJNe7Ej1rcbzqTTQUvLShZaWazebsVR",
  "5cjcW9wExnJJiqgLjq7DEG75Pm6JBgE1hNv4B2vHXUW6",
  "EHAAiTxcdDwQ3U4bU6YcMsQGaekdzLS3B5SmYo46kJtL",
  "5eHhjP8JaYkz83CWwvGU2uMUXefd3AazWGx4gpcuEEYD",
  "A7hAgCzFw14fejgCp387JUJRMNyz4j89JKnhtKU8piqW",
];

let blockhashCache = null;
let blockhashPromise = null;
const altCache = new Map();
const sleep = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

function hashIndex(seed, length) {
  let hash = 0;
  for (const character of String(seed || "e4")) hash = ((hash * 31) + character.charCodeAt(0)) >>> 0;
  return hash % length;
}

function pick(values, request, salt = "") {
  return new PublicKey(values[hashIndex(`${salt}:${request.request_id || request.mint || request.public_key}`, values.length)]);
}

function finite(value) {
  const result = Number(value);
  return Number.isFinite(result) ? result : null;
}

function positiveBigInt(value, label) {
  const result = finite(value);
  if (result === null || result <= 0) throw new Error(`invalid ${label}`);
  return BigInt(Math.floor(result));
}

function reserveLamports(value, label) {
  const result = finite(value);
  if (result === null || result <= 0) throw new Error(`missing ${label}`);
  return BigInt(Math.floor(result < 1_000_000 ? result * 1_000_000_000 : result));
}

function reserveTokens(value, label) {
  const result = finite(value);
  if (result === null || result <= 0) throw new Error(`missing ${label}`);
  const scale = Number(10n ** TOKEN_DECIMALS);
  return BigInt(Math.floor(result < 10_000_000_000 ? result * scale : result));
}

function feeBps(metadata) {
  const explicitTotal = finite(metadata.total_fee_bps ?? metadata.fee_bps);
  if (explicitTotal !== null && explicitTotal >= 0) return BigInt(Math.floor(explicitTotal));
  const protocol = Math.max(0, finite(metadata.protocol_fee_bps) ?? Number(process.env.E4_PUMP_PROTOCOL_FEE_BPS || 100));
  const creator = Math.max(0, finite(metadata.creator_fee_bps) ?? Number(process.env.E4_PUMP_CREATOR_FEE_BPS || 25));
  return BigInt(Math.floor(protocol + creator));
}

async function fetchWithRetry(url, options) {
  let lastError;
  for (let attempt = 0; attempt < BUILD_RETRIES; attempt += 1) {
    try {
      const response = await fetch(url, {...options, signal: AbortSignal.timeout(BUILD_TIMEOUT_MS)});
      if (response.status === 429 || response.status >= 500) {
        throw new Error(`HTTP ${response.status}: ${(await response.text()).slice(0, 500)}`);
      }
      return response;
    } catch (error) {
      lastError = error;
      if (attempt + 1 < BUILD_RETRIES) await sleep(Math.min(800, 75 * 2 ** attempt));
    }
  }
  throw lastError || new Error("request exhausted retries");
}

async function refreshBlockhash(force = false) {
  const now = Date.now();
  if (!force && blockhashCache && now - blockhashCache.refreshedAt < 15_000) return blockhashCache;
  if (blockhashPromise) return blockhashPromise;
  blockhashPromise = (async () => {
    let lastError;
    for (let attempt = 0; attempt < RPC_RETRIES; attempt += 1) {
      for (const connection of connections) {
        try {
          const result = await connection.getLatestBlockhash("processed");
          blockhashCache = {...result, refreshedAt: Date.now()};
          return blockhashCache;
        } catch (error) {
          lastError = error;
        }
      }
      await sleep(Math.min(500, 50 * 2 ** attempt));
    }
    throw lastError || new Error("all RPCs failed to return a blockhash");
  })();
  try {
    return await blockhashPromise;
  } finally {
    blockhashPromise = null;
  }
}

async function blockhashFor(request) {
  const supplied = String(request.metadata?.recent_blockhash || "").trim();
  if (supplied) return supplied;
  return (await refreshBlockhash(false)).blockhash;
}

setInterval(() => refreshBlockhash(true).catch((error) => process.stderr.write(`E4 blockhash refresh failed: ${String(error)}\n`)), 10_000).unref();
refreshBlockhash().catch(() => {});

async function lookupTable(key) {
  const cacheKey = key.toBase58();
  if (altCache.has(cacheKey)) return altCache.get(cacheKey);
  let lastError;
  for (let attempt = 0; attempt < RPC_RETRIES; attempt += 1) {
    for (const connection of connections) {
      try {
        const result = await connection.getAddressLookupTable(key);
        if (result.value) {
          altCache.set(cacheKey, result.value);
          return result.value;
        }
      } catch (error) {
        lastError = error;
      }
    }
    await sleep(Math.min(500, 50 * 2 ** attempt));
  }
  throw lastError || new Error(`address lookup table unavailable: ${cacheKey}`);
}

function computeBudget(request) {
  const instructions = [ComputeBudgetProgram.setComputeUnitLimit({units: COMPUTE_UNITS})];
  const prioritySol = Math.max(0, Number(request.priority_fee_sol || 0));
  if (prioritySol > 0) {
    const microLamports = Math.max(1, Math.floor((prioritySol * 1_000_000_000 * 1_000_000) / COMPUTE_UNITS));
    instructions.push(ComputeBudgetProgram.setComputeUnitPrice({microLamports}));
  }
  return instructions;
}

function appendTip(instructions, request) {
  const tipSol = Math.max(0, Number(request.tip_sol || 0));
  if (tipSol <= 0) return null;
  const account = pick(JITO_TIP_ACCOUNTS, request, "tip");
  instructions.push(SystemProgram.transfer({
    fromPubkey: new PublicKey(request.public_key),
    toPubkey: account,
    lamports: Math.max(1_000, Math.floor(tipSol * 1_000_000_000)),
  }));
  return account;
}

function compile(request, instructions, blockhash) {
  const message = new TransactionMessage({
    payerKey: new PublicKey(request.public_key),
    recentBlockhash: blockhash,
    instructions,
  }).compileToV0Message();
  const transaction = new VersionedTransaction(message);
  const bytes = Buffer.from(transaction.serialize());
  if (bytes.length > MAX_WIRE_BYTES) throw new Error(`transaction exceeds Solana wire limit: ${bytes.length}`);
  return bytes;
}

function metadataFor(request) {
  const metadata = request.metadata || {};
  if (!metadata.creator) throw new Error("local Pump build requires metadata.creator");
  if (!metadata.virtual_sol_reserves || !metadata.virtual_token_reserves || !metadata.real_token_reserves) {
    throw new Error("local Pump build requires current curve reserves");
  }
  return metadata;
}

function tokenProgramFor(metadata) {
  const value = String(metadata.token_program || "").trim();
  if (value === TOKEN_PROGRAM_ID.toBase58()) return TOKEN_PROGRAM_ID;
  return TOKEN_2022_PROGRAM_ID;
}

function quoteBuy(request, metadata) {
  const maxCost = positiveBigInt(Number(request.amount) * 1_000_000_000, "buy amount");
  const slippage = BigInt(Math.max(0, Math.min(9_000, Number(request.slippage_bps || 0))));
  const curveBudget = (maxCost * 10_000n) / (10_000n + slippage);
  const fees = feeBps(metadata);
  const input = curveBudget > 1n ? ((curveBudget - 1n) * 10_000n) / (10_000n + fees) : 0n;
  const virtualSol = reserveLamports(metadata.virtual_sol_reserves, "virtual SOL reserves");
  const virtualTokens = reserveTokens(metadata.virtual_token_reserves, "virtual token reserves");
  const realTokens = reserveTokens(metadata.real_token_reserves, "real token reserves");
  let tokens = input > 0n ? (input * virtualTokens) / (virtualSol + input) : 0n;
  if (tokens > realTokens) tokens = realTokens;
  if (tokens <= 0n) throw new Error("local buy quote produced zero tokens");
  return {tokens, maxCost};
}

function quoteSell(request, metadata) {
  const tokens = reserveTokens(request.amount, "sell token amount");
  const virtualSol = reserveLamports(metadata.virtual_sol_reserves, "virtual SOL reserves");
  const virtualTokens = reserveTokens(metadata.virtual_token_reserves, "virtual token reserves");
  const gross = (tokens * virtualSol) / (virtualTokens + tokens);
  const afterFee = gross - (gross * feeBps(metadata)) / 10_000n;
  const slippage = BigInt(Math.max(0, Math.min(9_000, Number(request.slippage_bps || 0))));
  const minimumSol = (afterFee * (10_000n - slippage)) / 10_000n;
  if (minimumSol <= 0n) throw new Error("local sell quote produced zero output");
  return {tokens, minimumSol};
}

async function buildPumpLocal(request) {
  if (!LOCAL_ENABLED) throw new Error("local Pump builder disabled");
  if (String(request.pool || "pump") !== "pump") throw new Error("local builder handles the Pump bonding curve only");
  const metadata = metadataFor(request);
  const user = new PublicKey(request.public_key);
  const mint = new PublicKey(request.mint);
  const creator = new PublicKey(metadata.creator);
  const tokenProgram = tokenProgramFor(metadata);
  const recipients = metadata.mayhem ? MAYHEM_FEE_RECIPIENTS : NORMAL_FEE_RECIPIENTS;
  const feeRecipient = pick(recipients, request, "fee");
  const buybackFeeRecipient = pick(BUYBACK_FEE_RECIPIENTS, request, "buyback");
  const instructions = computeBudget(request);

  if (String(request.side).toUpperCase() === "BUY") {
    const quote = quoteBuy(request, metadata);
    instructions.push(await PUMP_SDK.getBuyV2InstructionRaw({
      user,
      mint,
      creator,
      amount: new BN(quote.tokens.toString()),
      quoteAmount: new BN(quote.maxCost.toString()),
      tokenProgram,
      quoteMint: NATIVE_MINT,
      quoteTokenProgram: TOKEN_PROGRAM_ID,
      feeRecipient,
      buybackFeeRecipient,
    }));
  } else if (String(request.side).toUpperCase() === "SELL") {
    const quote = quoteSell(request, metadata);
    instructions.push(await PUMP_SDK.getSellV2InstructionRaw({
      user,
      mint,
      creator,
      amount: new BN(quote.tokens.toString()),
      quoteAmount: new BN(quote.minimumSol.toString()),
      tokenProgram,
      quoteMint: NATIVE_MINT,
      quoteTokenProgram: TOKEN_PROGRAM_ID,
      feeRecipient,
      buybackFeeRecipient,
    }));
  } else {
    throw new Error(`unsupported local Pump side: ${request.side}`);
  }

  const tip = appendTip(instructions, request);
  const bytes = compile(request, instructions, await blockhashFor(request));
  return {
    transaction_base64: bytes.toString("base64"),
    tip_appended: Boolean(tip),
    tip_account: tip?.toBase58() || null,
    builder_mode: "official-local-pump-sdk",
  };
}

async function buildSweep(request) {
  const destination = request.metadata?.destination;
  if (!destination) throw new Error("SWEEP request requires metadata.destination");
  const lamports = Math.floor(Number(request.amount) * 1_000_000_000);
  if (!Number.isSafeInteger(lamports) || lamports <= 0) throw new Error("invalid sweep amount");
  const instructions = computeBudget(request);
  instructions.push(SystemProgram.transfer({
    fromPubkey: new PublicKey(request.public_key),
    toPubkey: new PublicKey(destination),
    lamports,
  }));
  const tip = appendTip(instructions, request);
  const bytes = compile(request, instructions, await blockhashFor(request));
  return {
    transaction_base64: bytes.toString("base64"),
    tip_appended: Boolean(tip),
    tip_account: tip?.toBase58() || null,
    builder_mode: "local-system-transfer",
  };
}

async function appendTipToRemote(raw, request) {
  const tipSol = Math.max(0, Number(request.tip_sol || 0));
  if (tipSol <= 0) return {transaction_base64: raw.toString("base64"), tip_appended: false, tip_account: null, builder_mode: "remote-pumpportal-fallback"};
  const transaction = VersionedTransaction.deserialize(raw);
  const lookups = [];
  for (const item of transaction.message.addressTableLookups || []) lookups.push(await lookupTable(item.accountKey));
  const message = TransactionMessage.decompile(transaction.message, {addressLookupTableAccounts: lookups});
  const tip = pick(JITO_TIP_ACCOUNTS, request, "tip");
  message.instructions.push(SystemProgram.transfer({
    fromPubkey: new PublicKey(request.public_key),
    toPubkey: tip,
    lamports: Math.max(1_000, Math.floor(tipSol * 1_000_000_000)),
  }));
  const bytes = Buffer.from(new VersionedTransaction(message.compileToV0Message(lookups)).serialize());
  if (bytes.length > MAX_WIRE_BYTES) throw new Error(`remote transaction with tip exceeds wire limit: ${bytes.length}`);
  return {transaction_base64: bytes.toString("base64"), tip_appended: true, tip_account: tip.toBase58(), builder_mode: "remote-pumpportal-fallback"};
}

async function buildPumpRemote(request) {
  const response = await fetchWithRetry(TRADE_LOCAL_URL, {
    method: "POST",
    headers: {"content-type": "application/json"},
    body: JSON.stringify({
      publicKey: request.public_key,
      action: String(request.side).toLowerCase(),
      mint: request.mint,
      amount: Number(request.amount),
      denominatedInSol: request.denominated_in_sol ? "true" : "false",
      slippage: Number(request.slippage_bps) / 100,
      priorityFee: Number(request.priority_fee_sol || 0),
      pool: request.pool || "pump",
    }),
  });
  if (!response.ok) throw new Error(`trade-local HTTP ${response.status}: ${(await response.text()).slice(0, 500)}`);
  const bytes = Buffer.from(await response.arrayBuffer());
  if (!bytes.length) throw new Error("trade-local returned an empty transaction");
  return appendTipToRemote(bytes, request);
}

async function buildPump(request) {
  const started = process.hrtime.bigint();
  let localError;
  try {
    const result = await buildPumpLocal(request);
    return {...result, build_ns: Number(process.hrtime.bigint() - started)};
  } catch (error) {
    localError = error;
    if (!REMOTE_FALLBACK) throw error;
  }
  const result = await buildPumpRemote(request);
  return {...result, local_error: String(localError), build_ns: Number(process.hrtime.bigint() - started)};
}

async function handle(line) {
  const request = JSON.parse(line);
  const result = request.side === "SWEEP" ? await buildSweep(request) : await buildPump(request);
  return {request_id: request.request_id || null, ...result};
}

async function selfTest() {
  const request = {
    request_id: "local-self-test",
    side: "BUY",
    mint: "3hCyCV1JhuF6Rup98djLbh1fyKxHyQjTcTGEQcA1pump",
    public_key: "E4EzXdwf7NNdqM2XGswWaWHfxgucVCo24PTCcrimTKBz",
    amount: 0.01,
    denominated_in_sol: true,
    slippage_bps: 800,
    priority_fee_sol: 0,
    tip_sol: 0,
    pool: "pump",
    metadata: {
      creator: "D9gQ6RhKEpnobPBUdWY5bPQt2p3zGk3iVz6ChpUi2ArA",
      virtual_sol_reserves: 30_000_000_000,
      virtual_token_reserves: 1_073_000_000_000_000,
      real_token_reserves: 793_100_000_000_000,
      recent_blockhash: "11111111111111111111111111111111",
      token_program: TOKEN_2022_PROGRAM_ID.toBase58(),
    },
  };
  const result = await buildPumpLocal(request);
  const bytes = Buffer.from(result.transaction_base64, "base64");
  process.stdout.write(`${JSON.stringify({...result, wire_bytes: bytes.length})}\n`);
}

if (process.argv.includes("--self-test")) {
  await selfTest();
  process.exit(0);
}

const input = readline.createInterface({input: process.stdin, crlfDelay: Infinity});
for await (const raw of input) {
  const line = raw.trim();
  if (!line) continue;
  try {
    process.stdout.write(`${JSON.stringify(await handle(line))}\n`);
  } catch (error) {
    let request_id = null;
    try { request_id = JSON.parse(line).request_id || null; } catch {}
    process.stdout.write(`${JSON.stringify({request_id, error: error?.stack || String(error)})}\n`);
  }
}
