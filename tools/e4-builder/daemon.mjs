#!/usr/bin/env node
import readline from "node:readline";
import BN from "bn.js";
import {PUMP_SDK} from "@pump-fun/pump-sdk";
import {
  NATIVE_MINT,
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
const connections = RPC_URLS.map((url) => new Connection(url, "processed"));
const TRADE_LOCAL_URL = process.env.E4_PUMPPORTAL_TRADE_LOCAL_URL || "https://pumpportal.fun/api/trade-local";
const BUILD_TIMEOUT_MS = Number(process.env.E4_BUILDER_TIMEOUT_MS || 2200);
const BUILD_RETRIES = Math.max(1, Math.min(4, Number(process.env.E4_BUILDER_RETRIES || 3)));
const RPC_RETRIES = Math.max(1, Math.min(4, Number(process.env.E4_ALT_RPC_RETRIES || 3)));
const MAX_WIRE_BYTES = 1232;
const DEFAULT_COMPUTE_UNITS = Math.max(150_000, Number(process.env.E4_LOCAL_COMPUTE_UNITS || 360_000));
const PROTOCOL_FEE_BPS = BigInt(Math.max(0, Number(process.env.E4_PUMP_PROTOCOL_FEE_BPS || 100)));
const CREATOR_FEE_BPS = BigInt(Math.max(0, Number(process.env.E4_PUMP_CREATOR_FEE_BPS || 25)));
const LOCAL_BUILDER_ENABLED = String(process.env.E4_LOCAL_PUMP_BUILDER || "true").toLowerCase() !== "false";
const REMOTE_FALLBACK_ENABLED = String(process.env.E4_REMOTE_BUILDER_FALLBACK || "true").toLowerCase() !== "false";
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
const FEE_RECIPIENTS = [
  "62qc2CNXwrYqQScmEdiZFFAnJR262PxWEuNQtxfafNgV",
  "7VtfL8fvgNfhz17qKRMjzQEXgbdpnHHHQRh54R9jP2RJ",
  "7hTckgnGnLQR6sdH7YkqFTAA7VwTfYFaZ6EhEsU3saCX",
  "9rPYyANsfQZw3DnDmKE3YCQF5E8oD89UXoHn9JFEhJUz",
  "AVmoTthdrX6tKt4nDjco2D775W2YK3sDhxPcMmzUAmTY",
  "CebN5WGQ4jvEPvsVU4EoHEpgzq1VV2h1mGqmFPzZLhUR",
  "FWsW1xNtWscwNmKv6wVsU1iTzRN6wmmk3MjxRP5tT7hz",
  "G5UZAVbAf46s7cKWoyKu8kYTip9DGTpbLZ2qa9Aq69dP",
];
const BUYBACK_FEE_RECIPIENTS = [
  "5YxQYdDFh4X5qoaUgC4R2eLuETbrZrTadpfntSMu5FjQ",
  "9M4gC63vG4vSpyoQF2wQEqXTEjC3BmTMLpY8XSpAHZGo",
  "GXPFv5UXU6pD4CkTGqR7qbCHheTzH7gGJkZ8N3tZ9bRH",
  "3BpXYkYuHiV4Q4N3FjX4KRAu6JQTs4bmPiXh5nYqj5V9",
  "5cjc2K3SBDWYdfpBkAq13hDh8P1XfUQv5PjQNhUKu99G",
  "EHAAtvA2q7tU8KZsV6P2M1iKjo1rwgz5P7v2mK6u9vVL",
  "5eHh8cNvtRsNHZVLc6c5nn6Q8G3hA4RopTzQmCjW4xVH",
  "A7hA7xQ3mVbDqLrFQh8K9Wn3sZ2pT6uC5jM4eR1yG8kN",
];
const altCache = new Map();
let blockhashCache = null;
let blockhashPromise = null;

function hashIndex(seed, length) {
  let hash = 0;
  for (const character of String(seed || "e4")) hash = ((hash * 31) + character.charCodeAt(0)) >>> 0;
  return hash % length;
}

function deterministicTipAccount(request) {
  return new PublicKey(JITO_TIP_ACCOUNTS[hashIndex(request.request_id || request.mint || request.public_key, JITO_TIP_ACCOUNTS.length)]);
}

function deterministicFeeRecipient(request) {
  return new PublicKey(FEE_RECIPIENTS[hashIndex(request.mint || request.request_id, FEE_RECIPIENTS.length)]);
}

function deterministicBuybackRecipient(request) {
  return new PublicKey(BUYBACK_FEE_RECIPIENTS[hashIndex(request.request_id || request.mint, BUYBACK_FEE_RECIPIENTS.length)]);
}

const sleep = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

function finiteNumber(value) {
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function integerBigInt(value, label) {
  const number = finiteNumber(value);
  if (number === null || number <= 0) throw new Error(`invalid ${label}`);
  return BigInt(Math.floor(number));
}

function rawLamports(value, label) {
  const number = finiteNumber(value);
  if (number === null || number <= 0) throw new Error(`missing ${label}`);
  return BigInt(Math.floor(number < 1_000_000 ? number * 1_000_000_000 : number));
}

function rawTokens(value, label) {
  const number = finiteNumber(value);
  if (number === null || number <= 0) throw new Error(`missing ${label}`);
  const scale = Number(10n ** TOKEN_DECIMALS);
  return BigInt(Math.floor(number < 10_000_000_000 ? number * scale : number));
}

async function fetchWithRetry(url, options) {
  let lastError;
  for (let attempt = 0; attempt < BUILD_RETRIES; attempt += 1) {
    try {
      const response = await fetch(url, {
        ...options,
        signal: AbortSignal.timeout(BUILD_TIMEOUT_MS),
      });
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

async function latestBlockhash() {
  return refreshBlockhash(false);
}

setInterval(() => {
  refreshBlockhash(true).catch((error) => process.stderr.write(`E4 blockhash refresh failed: ${String(error)}\n`));
}, 10_000).unref();
refreshBlockhash().catch(() => {});

async function getLookupTable(key) {
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

function computeBudgetInstructions(request) {
  const instructions = [ComputeBudgetProgram.setComputeUnitLimit({units: DEFAULT_COMPUTE_UNITS})];
  const priorityFeeSol = Math.max(0, Number(request.priority_fee_sol || 0));
  if (priorityFeeSol > 0) {
    const lamports = priorityFeeSol * 1_000_000_000;
    const microLamports = Math.max(1, Math.floor((lamports * 1_000_000) / DEFAULT_COMPUTE_UNITS));
    instructions.push(ComputeBudgetProgram.setComputeUnitPrice({microLamports}));
  }
  return instructions;
}

function appendTipInstruction(instructions, request) {
  const requestedTip = Math.max(0, Number(request.tip_sol || 0));
  if (requestedTip <= 0) return null;
  const account = deterministicTipAccount(request);
  instructions.push(SystemProgram.transfer({
    fromPubkey: new PublicKey(request.public_key),
    toPubkey: account,
    lamports: Math.max(1_000, Math.floor(requestedTip * 1_000_000_000)),
  }));
  return account;
}

function compileLocal(request, instructions, blockhash) {
  const payer = new PublicKey(request.public_key);
  const message = new TransactionMessage({payerKey: payer, recentBlockhash: blockhash, instructions}).compileToV0Message();
  const transaction = new VersionedTransaction(message);
  const bytes = Buffer.from(transaction.serialize());
  if (bytes.length > MAX_WIRE_BYTES) throw new Error(`local Pump transaction exceeds Solana wire limit: ${bytes.length}`);
  return bytes;
}

function localMetadata(request) {
  const metadata = request.metadata || {};
  if (!metadata.creator) throw new Error("local Pump build requires metadata.creator");
  if (!metadata.virtual_sol_reserves || !metadata.virtual_token_reserves || !metadata.real_token_reserves) {
    throw new Error("local Pump build requires current curve reserves");
  }
  if (metadata.mayhem) throw new Error("mayhem mode currently uses remote builder fallback");
  return metadata;
}

function tokenProgramFromMetadata(metadata) {
  const value = String(metadata.token_program || "").trim();
  if (value && value === TOKEN_PROGRAM_ID.toBase58()) return TOKEN_PROGRAM_ID;
  if (value && value === TOKEN_2022_PROGRAM_ID.toBase58()) return TOKEN_2022_PROGRAM_ID;
  // Current CreateV2 Pump launches in the captured live sample use Token-2022.
  return TOKEN_2022_PROGRAM_ID;
}

function buyQuote(request, metadata) {
  const maxCost = integerBigInt(Number(request.amount) * 1_000_000_000, "buy amount");
  const slippageBps = BigInt(Math.max(0, Math.min(9_000, Number(request.slippage_bps || 0))));
  const curveBudget = (maxCost * 10_000n) / (10_000n + slippageBps);
  const feeBps = PROTOCOL_FEE_BPS + CREATOR_FEE_BPS;
  const input = curveBudget > 1n ? ((curveBudget - 1n) * 10_000n) / (10_000n + feeBps) : 0n;
  const virtualSol = rawLamports(metadata.virtual_sol_reserves, "virtual SOL reserves");
  const virtualTokens = rawTokens(metadata.virtual_token_reserves, "virtual token reserves");
  const realTokens = rawTokens(metadata.real_token_reserves, "real token reserves");
  let tokenAmount = input > 0n ? (input * virtualTokens) / (virtualSol + input) : 0n;
  if (tokenAmount > realTokens) tokenAmount = realTokens;
  if (tokenAmount <= 0n) throw new Error("local buy quote produced zero tokens");
  return {tokenAmount, maxCost};
}

function sellQuote(request, metadata) {
  const amount = rawTokens(request.amount, "sell token amount");
  const virtualSol = rawLamports(metadata.virtual_sol_reserves, "virtual SOL reserves");
  const virtualTokens = rawTokens(metadata.virtual_token_reserves, "virtual token reserves");
  const rawSol = (amount * virtualSol) / (virtualTokens + amount);
  const feeBps = PROTOCOL_FEE_BPS + CREATOR_FEE_BPS;
  const afterFee = rawSol - (rawSol * feeBps) / 10_000n;
  const slippageBps = BigInt(Math.max(0, Math.min(9_000, Number(request.slippage_bps || 0))));
  const minimumSol = (afterFee * (10_000n - slippageBps)) / 10_000n;
  if (minimumSol <= 0n) throw new Error("local sell quote produced zero minimum output");
  return {tokenAmount: amount, minimumSol};
}

async function buildPumpLocal(request) {
  if (!LOCAL_BUILDER_ENABLED) throw new Error("local Pump builder disabled");
  if (String(request.pool || "pump") !== "pump") throw new Error("local builder handles bonding-curve Pump trades only");
  const metadata = localMetadata(request);
  const user = new PublicKey(request.public_key);
  const mint = new PublicKey(request.mint);
  const creator = new PublicKey(metadata.creator);
  const tokenProgram = tokenProgramFromMetadata(metadata);
  const feeRecipient = deterministicFeeRecipient(request);
  const buybackFeeRecipient = deterministicBuybackRecipient(request);
  const instructions = computeBudgetInstructions(request);

  if (String(request.side).toUpperCase() === "BUY") {
    const quote = buyQuote(request, metadata);
    instructions.push(await PUMP_SDK.getBuyV2InstructionRaw({
      user,
      mint,
      creator,
      amount: new BN(quote.tokenAmount.toString()),
      quoteAmount: new BN(quote.maxCost.toString()),
      tokenProgram,
      quoteMint: NATIVE_MINT,
      quoteTokenProgram: TOKEN_PROGRAM_ID,
      feeRecipient,
      buybackFeeRecipient,
    }));
  } else if (String(request.side).toUpperCase() === "SELL") {
    const quote = sellQuote(request, metadata);
    instructions.push(await PUMP_SDK.getSellV2InstructionRaw({
      user,
      mint,
      creator,
      amount: new BN(quote.tokenAmount.toString()),
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

  const tipAccount = appendTipInstruction(instructions, request);
  const {blockhash} = await latestBlockhash();
  const bytes = compileLocal(request, instructions, blockhash);
  return {
    transaction_base64: bytes.toString("base64"),
    tip_appended: Boolean(tipAccount),
    tip_account: tipAccount?.toBase58() || null,
    builder_mode: "official-local-pump-sdk",
  };
}

async function buildSweep(request) {
  const destination = request.metadata?.destination;
  if (!destination) throw new Error("SWEEP request requires metadata.destination");
  const from = new PublicKey(request.public_key);
  const to = new PublicKey(destination);
  const lamports = Math.floor(Number(request.amount) * 1_000_000_000);
  if (!Number.isSafeInteger(lamports) || lamports <= 0) throw new Error("invalid sweep amount");
  const {blockhash} = await latestBlockhash();
  const instructions = computeBudgetInstructions(request);
  instructions.push(SystemProgram.transfer({fromPubkey: from, toPubkey: to, lamports}));
  const tipAccount = appendTipInstruction(instructions, request);
  const bytes = compileLocal(request, instructions, blockhash);
  return {
    transaction_base64: bytes.toString("base64"),
    tip_appended: Boolean(tipAccount),
    tip_account: tipAccount?.toBase58() || null,
    builder_mode: "local-system-transfer",
  };
}

async function appendJitoTip(raw, request) {
  const requestedSol = Math.max(0, Number(request.tip_sol || 0));
  if (requestedSol <= 0) {
    return {transaction_base64: raw.toString("base64"), tip_appended: false, tip_account: null};
  }
  const transaction = VersionedTransaction.deserialize(raw);
  const lookups = [];
  for (const lookup of transaction.message.addressTableLookups || []) {
    lookups.push(await getLookupTable(lookup.accountKey));
  }
  const message = TransactionMessage.decompile(transaction.message, {addressLookupTableAccounts: lookups});
  const tipAccount = deterministicTipAccount(request);
  const lamports = Math.max(1_000, Math.floor(requestedSol * 1_000_000_000));
  message.instructions.push(SystemProgram.transfer({
    fromPubkey: new PublicKey(request.public_key),
    toPubkey: tipAccount,
    lamports,
  }));
  const compiled = message.compileToV0Message(lookups);
  const rebuilt = new VersionedTransaction(compiled);
  const bytes = Buffer.from(rebuilt.serialize());
  if (bytes.length > MAX_WIRE_BYTES) throw new Error(`transaction with Jito tip exceeds Solana wire limit: ${bytes.length}`);
  return {
    transaction_base64: bytes.toString("base64"),
    tip_appended: true,
    tip_account: tipAccount.toBase58(),
    tip_lamports: lamports,
    builder_mode: "remote-pumpportal-fallback",
  };
}

async function buildPumpRemote(request) {
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
  return appendJitoTip(bytes, request);
}

async function buildPump(request) {
  const started = process.hrtime.bigint();
  let localError;
  try {
    const built = await buildPumpLocal(request);
    return {...built, build_ns: Number(process.hrtime.bigint() - started)};
  } catch (error) {
    localError = error;
    if (!REMOTE_FALLBACK_ENABLED) throw error;
  }
  const built = await buildPumpRemote(request);
  return {
    ...built,
    local_error: String(localError || "unknown local builder failure"),
    build_ns: Number(process.hrtime.bigint() - started),
  };
}

async function handle(line) {
  const request = JSON.parse(line);
  const built = request.side === "SWEEP" ? await buildSweep(request) : await buildPump(request);
  return {request_id: request.request_id || null, ...built};
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
