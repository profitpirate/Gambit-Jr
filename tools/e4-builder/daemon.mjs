#!/usr/bin/env node
import readline from "node:readline";
import {
  Connection,
  PublicKey,
  SystemProgram,
  TransactionMessage,
  VersionedTransaction,
} from "@solana/web3.js";

const RPC_URL = process.env.E4_PRIMARY_RPC_URL || process.env.SOLANA_RPC_URL || "https://api.mainnet-beta.solana.com";
const TRADE_LOCAL_URL = process.env.E4_PUMPPORTAL_TRADE_LOCAL_URL || "https://pumpportal.fun/api/trade-local";
const BUILD_TIMEOUT_MS = Number(process.env.E4_BUILDER_TIMEOUT_MS || 1800);
const connection = new Connection(RPC_URL, "processed");

async function buildSweep(request) {
  const destination = request.metadata?.destination;
  if (!destination) throw new Error("SWEEP request requires metadata.destination");
  const from = new PublicKey(request.public_key);
  const to = new PublicKey(destination);
  const lamports = Math.floor(Number(request.amount) * 1_000_000_000);
  if (!Number.isSafeInteger(lamports) || lamports <= 0) throw new Error("invalid sweep amount");
  const { blockhash } = await connection.getLatestBlockhash("processed");
  const message = new TransactionMessage({
    payerKey: from,
    recentBlockhash: blockhash,
    instructions: [SystemProgram.transfer({ fromPubkey: from, toPubkey: to, lamports })],
  }).compileToV0Message();
  return Buffer.from(new VersionedTransaction(message).serialize()).toString("base64");
}

async function buildPump(request) {
  const priorityBudget = Number(request.priority_fee_sol || 0) + Number(request.tip_sol || 0);
  const payload = {
    publicKey: request.public_key,
    action: String(request.side).toLowerCase(),
    mint: request.mint,
    amount: Number(request.amount),
    denominatedInSol: request.denominated_in_sol ? "true" : "false",
    slippage: Number(request.slippage_bps) / 100,
    priorityFee: priorityBudget,
    pool: request.pool || "auto",
  };
  const response = await fetch(TRADE_LOCAL_URL, {
    method: "POST",
    headers: {"content-type": "application/json"},
    body: JSON.stringify(payload),
    signal: AbortSignal.timeout(BUILD_TIMEOUT_MS),
  });
  if (!response.ok) {
    throw new Error(`trade-local HTTP ${response.status}: ${(await response.text()).slice(0, 500)}`);
  }
  const bytes = Buffer.from(await response.arrayBuffer());
  if (!bytes.length) throw new Error("trade-local returned an empty transaction");
  return bytes.toString("base64");
}

async function handle(line) {
  const request = JSON.parse(line);
  const transaction_base64 = request.side === "SWEEP" ? await buildSweep(request) : await buildPump(request);
  return {request_id: request.request_id || null, transaction_base64};
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
