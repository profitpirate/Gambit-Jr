#!/usr/bin/env node
import {
  Connection,
  PublicKey,
  SystemProgram,
  TransactionMessage,
  VersionedTransaction,
} from "@solana/web3.js";

const RPC_URL = process.env.E4_PRIMARY_RPC_URL || process.env.SOLANA_RPC_URL || "https://api.mainnet-beta.solana.com";
const TRADE_LOCAL_URL = process.env.E4_PUMPPORTAL_TRADE_LOCAL_URL || "https://pumpportal.fun/api/trade-local";
const connection = new Connection(RPC_URL, "processed");

function fail(message) {
  process.stderr.write(`${message}\n`);
  process.exit(1);
}

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
  const transaction = new VersionedTransaction(message);
  return Buffer.from(transaction.serialize()).toString("base64");
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
    pool: request.pool || "auto",
  };
  const response = await fetch(TRADE_LOCAL_URL, {
    method: "POST",
    headers: {"content-type": "application/json"},
    body: JSON.stringify(payload),
    signal: AbortSignal.timeout(Number(process.env.E4_BUILDER_TIMEOUT_MS || 1800)),
  });
  if (!response.ok) {
    throw new Error(`trade-local HTTP ${response.status}: ${(await response.text()).slice(0, 500)}`);
  }
  const bytes = Buffer.from(await response.arrayBuffer());
  if (!bytes.length) throw new Error("trade-local returned an empty transaction");
  return bytes.toString("base64");
}

let input = "";
for await (const chunk of process.stdin) input += chunk;
const line = input.trim().split(/\r?\n/).find(Boolean);
if (!line) fail("missing JSON request");

try {
  const request = JSON.parse(line);
  const transaction_base64 = request.side === "SWEEP" ? await buildSweep(request) : await buildPump(request);
  process.stdout.write(JSON.stringify({transaction_base64}));
} catch (error) {
  fail(error?.stack || String(error));
}
