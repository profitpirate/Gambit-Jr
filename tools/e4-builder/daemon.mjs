#!/usr/bin/env node
import readline from "node:readline";
import {
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
const ALT_RETRIES = Math.max(1, Math.min(4, Number(process.env.E4_ALT_RPC_RETRIES || 3)));
const MAX_WIRE_BYTES = 1232;
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
const altCache = new Map();

function deterministicTipAccount(request) {
  const seed = String(request.request_id || request.mint || request.public_key || "e4");
  let hash = 0;
  for (const character of seed) hash = ((hash * 31) + character.charCodeAt(0)) >>> 0;
  return new PublicKey(JITO_TIP_ACCOUNTS[hash % JITO_TIP_ACCOUNTS.length]);
}

const sleep = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

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

async function latestBlockhash() {
  let lastError;
  for (let attempt = 0; attempt < ALT_RETRIES; attempt += 1) {
    for (const connection of connections) {
      try {
        return await connection.getLatestBlockhash("processed");
      } catch (error) {
        lastError = error;
      }
    }
    await sleep(Math.min(800, 75 * 2 ** attempt));
  }
  throw lastError || new Error("all RPCs failed to return a blockhash");
}

async function getLookupTable(key) {
  const cacheKey = key.toBase58();
  if (altCache.has(cacheKey)) return altCache.get(cacheKey);
  let lastError;
  for (let attempt = 0; attempt < ALT_RETRIES; attempt += 1) {
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
    await sleep(Math.min(800, 75 * 2 ** attempt));
  }
  throw lastError || new Error(`address lookup table unavailable: ${cacheKey}`);
}

async function buildSweep(request) {
  const destination = request.metadata?.destination;
  if (!destination) throw new Error("SWEEP request requires metadata.destination");
  const from = new PublicKey(request.public_key);
  const to = new PublicKey(destination);
  const lamports = Math.floor(Number(request.amount) * 1_000_000_000);
  if (!Number.isSafeInteger(lamports) || lamports <= 0) throw new Error("invalid sweep amount");
  const {blockhash} = await latestBlockhash();
  const instructions = [SystemProgram.transfer({fromPubkey: from, toPubkey: to, lamports})];
  const requestedTip = Math.max(0, Number(request.tip_sol || 0));
  if (requestedTip > 0) {
    instructions.push(SystemProgram.transfer({
      fromPubkey: from,
      toPubkey: deterministicTipAccount(request),
      lamports: Math.max(1_000, Math.floor(requestedTip * 1_000_000_000)),
    }));
  }
  const message = new TransactionMessage({payerKey: from, recentBlockhash: blockhash, instructions}).compileToV0Message();
  const transaction = new VersionedTransaction(message);
  return {
    transaction_base64: Buffer.from(transaction.serialize()).toString("base64"),
    tip_appended: requestedTip > 0,
    tip_account: requestedTip > 0 ? deterministicTipAccount(request).toBase58() : null,
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
  message.instructions.push(
    SystemProgram.transfer({
      fromPubkey: new PublicKey(request.public_key),
      toPubkey: tipAccount,
      lamports,
    }),
  );
  const compiled = message.compileToV0Message(lookups);
  const rebuilt = new VersionedTransaction(compiled);
  const bytes = Buffer.from(rebuilt.serialize());
  if (bytes.length > MAX_WIRE_BYTES) {
    throw new Error(`transaction with Jito tip exceeds Solana wire limit: ${bytes.length}`);
  }
  return {
    transaction_base64: bytes.toString("base64"),
    tip_appended: true,
    tip_account: tipAccount.toBase58(),
    tip_lamports: lamports,
  };
}

async function buildPump(request) {
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
  if (!response.ok) {
    throw new Error(`trade-local HTTP ${response.status}: ${(await response.text()).slice(0, 500)}`);
  }
  const bytes = Buffer.from(await response.arrayBuffer());
  if (!bytes.length) throw new Error("trade-local returned an empty transaction");
  return appendJitoTip(bytes, request);
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
