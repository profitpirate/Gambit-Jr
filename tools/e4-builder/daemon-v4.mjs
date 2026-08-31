#!/usr/bin/env node
import readline from "node:readline";
import {once} from "node:events";
import {spawn, spawnSync} from "node:child_process";
import {fileURLToPath} from "node:url";

const DAEMON_V3 = fileURLToPath(new URL("./daemon-v3.mjs", import.meta.url));
const BAD_TIP_ACCOUNT = "HFqU5x63VTqVss8hp11i4wVV8bD44PvwucfZ2bU7gRe";
const TIP_ACCOUNT_COUNT = 8;

function hashIndex(seed, length) {
  let hash = 0;
  for (const character of String(seed || "e4")) {
    hash = ((hash * 31) + character.charCodeAt(0)) >>> 0;
  }
  return hash % length;
}

function flattenMetadata(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return value || {};
  const nested = value.state_hint;
  if (!nested || typeof nested !== "object" || Array.isArray(nested)) return value;
  const {state_hint: _ignored, ...outer} = value;
  return {...nested, ...outer};
}

function normalizedRequest(request) {
  const originalId = request.request_id ?? null;
  const normalized = {
    ...request,
    metadata: flattenMetadata(request.metadata),
  };
  if (request.state_hint && typeof request.state_hint === "object") {
    normalized.state_hint = flattenMetadata(request.state_hint);
  }

  if (Number(normalized.tip_sol || 0) > 0) {
    const base = String(originalId || request.mint || request.public_key || "e4");
    let internalId = base;
    let attempt = 0;
    while (hashIndex(`tip:${internalId}`, TIP_ACCOUNT_COUNT) === 1) {
      attempt += 1;
      internalId = `${base}-valid-jito-${attempt}`;
    }
    normalized.request_id = internalId;
  }
  return {originalId, normalized};
}

function normalizedResponse(response, originalId, wrapperStartedNs) {
  const result = {...response, request_id: originalId};
  if (result.build_ms == null && result.build_ns != null) {
    result.build_ms = Number(result.build_ns) / 1_000_000;
  }
  if (typeof result.builder_mode === "string") {
    result.builder_mode = result.builder_mode.replace(/v3$/u, "v4");
  }
  result.builder_version = "local-v4";
  result.wrapper_roundtrip_ms = Number(process.hrtime.bigint() - wrapperStartedNs) / 1_000_000;
  if (result.tip_account === BAD_TIP_ACCOUNT) {
    throw new Error("invalid legacy Jito tip account selected");
  }
  return result;
}

function runSelfTest() {
  const execution = spawnSync(process.execPath, [DAEMON_V3, "--self-test"], {
    encoding: "utf8",
    env: process.env,
    timeout: 20_000,
  });
  if (execution.status !== 0) {
    process.stderr.write(execution.stderr || "daemon-v3 self-test failed\n");
    process.exit(execution.status || 1);
  }
  const line = String(execution.stdout || "").trim().split(/\r?\n/u).filter(Boolean).at(-1);
  const response = normalizedResponse(JSON.parse(line), "local-self-test", process.hrtime.bigint());
  process.stdout.write(`${JSON.stringify(response)}\n`);
}

if (process.argv.includes("--self-test")) {
  runSelfTest();
  process.exit(0);
}

const child = spawn(process.execPath, [DAEMON_V3], {
  stdio: ["pipe", "pipe", "pipe"],
  env: process.env,
});
child.stderr.pipe(process.stderr);
const childLines = readline.createInterface({input: child.stdout, crlfDelay: Infinity});
const childIterator = childLines[Symbol.asyncIterator]();
let shuttingDown = false;

child.on("exit", (code, signal) => {
  if (shuttingDown) return;
  process.stderr.write(`E4 builder v3 child exited code=${code} signal=${signal}\n`);
  process.exit(code || 1);
});

for (const signal of ["SIGINT", "SIGTERM"]) {
  process.on(signal, () => {
    shuttingDown = true;
    child.kill(signal);
    process.exit(0);
  });
}
process.on("exit", () => {
  shuttingDown = true;
  if (!child.killed) child.kill("SIGTERM");
});

async function askChild(payload) {
  const line = `${JSON.stringify(payload)}\n`;
  if (!child.stdin.write(line)) await once(child.stdin, "drain");
  const next = await childIterator.next();
  if (next.done || !next.value) throw new Error("builder v3 child closed stdout");
  return JSON.parse(next.value);
}

const input = readline.createInterface({input: process.stdin, crlfDelay: Infinity});
for await (const raw of input) {
  const line = raw.trim();
  if (!line) continue;
  let originalId = null;
  const started = process.hrtime.bigint();
  try {
    const request = JSON.parse(line);
    const prepared = normalizedRequest(request);
    originalId = prepared.originalId;
    const response = await askChild(prepared.normalized);
    process.stdout.write(`${JSON.stringify(normalizedResponse(response, originalId, started))}\n`);
  } catch (error) {
    process.stdout.write(`${JSON.stringify({
      request_id: originalId,
      builder_version: "local-v4",
      error: error?.stack || String(error),
    })}\n`);
  }
}

shuttingDown = true;
childLines.close();
child.stdin.end();
if (!child.killed) child.kill("SIGTERM");
await Promise.race([
  once(child, "exit"),
  new Promise((resolve) => setTimeout(resolve, 500)),
]);
