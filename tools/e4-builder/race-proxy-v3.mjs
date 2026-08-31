#!/usr/bin/env node
import {spawn} from "node:child_process";
import {existsSync} from "node:fs";
import {createInterface} from "node:readline";
import path from "node:path";
import {fileURLToPath} from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(here, "../..");
const daemon = path.join(here, "daemon-v2.mjs");

function resolvePreload(raw) {
  if (!raw) return path.join(here, "fast-preload-v4.mjs");
  if (path.isAbsolute(raw)) return raw;
  const candidates = [
    path.resolve(process.cwd(), raw),
    path.resolve(repoRoot, raw),
    path.resolve(here, raw),
    path.resolve(here, path.basename(raw)),
  ];
  const found = candidates.find((candidate) => existsSync(candidate));
  if (found) return found;
  // Return the repo-root interpretation so the error, if any, is stable and
  // meaningful instead of duplicating tools/e4-builder when cwd=here.
  return path.resolve(repoRoot, raw);
}

const preload = resolvePreload(process.env.E4_BUILDER_PRELOAD);

if (process.argv.includes("--self-test")) {
  const child = spawn(process.execPath, ["--import", preload, daemon, "--self-test"], {
    cwd: here,
    env: process.env,
    stdio: "inherit",
  });
  child.on("exit", (code, signal) => {
    if (signal) process.kill(process.pid, signal);
    process.exit(code ?? 1);
  });
} else {
  const childCount = Math.max(2, Math.min(4, Number(process.env.E4_BUILDER_RACE_CHILDREN || 2)));
  const children = [];
  const generationsByChild = new Map();
  const pending = new Map();
  let generation = 0;
  let shuttingDown = false;

  function validResponse(line) {
    try {
      const value = JSON.parse(line);
      if (!value || typeof value !== "object" || Array.isArray(value)) return false;
      if (value.error || value.ok === false || value.success === false) return false;
      // Different builder versions use different transaction field names. Any
      // non-error JSON object is a valid first response; Python performs the
      // authoritative schema validation before signing.
      return true;
    } catch {
      return false;
    }
  }

  function settle(id, line, valid) {
    const item = pending.get(id);
    if (!item) return;
    item.responses.push({line, valid});
    if (!item.emitted && valid) {
      item.emitted = true;
      process.stdout.write(line.endsWith("\n") ? line : `${line}\n`);
      clearTimeout(item.timer);
      // Keep the record briefly so a slower child response is drained against
      // the correct generation rather than contaminating a later request.
      item.cleanup = setTimeout(() => pending.delete(id), 5_000);
      item.cleanup.unref();
      return;
    }
    if (!item.emitted && item.responses.length >= children.length) {
      item.emitted = true;
      const preferred = item.responses.find((row) => row.valid) || item.responses[0];
      process.stdout.write(preferred.line.endsWith("\n") ? preferred.line : `${preferred.line}\n`);
      clearTimeout(item.timer);
      pending.delete(id);
    }
  }

  function startChild(index) {
    const child = spawn(process.execPath, ["--import", preload, daemon], {
      cwd: here,
      env: {...process.env, E4_BUILDER_RACE_INDEX: String(index)},
      stdio: ["pipe", "pipe", "pipe"],
    });
    children[index] = child;
    generationsByChild.set(index, []);
    const lines = createInterface({input: child.stdout, crlfDelay: Infinity});
    lines.on("line", (line) => {
      const queue = generationsByChild.get(index) || [];
      const id = queue.shift();
      if (id === undefined) {
        process.stderr.write(`[e4-builder-race:${index}] unsolicited response: ${line.slice(0, 300)}\n`);
        return;
      }
      settle(id, line, validResponse(line));
    });
    child.stderr.on("data", (chunk) => {
      process.stderr.write(`[e4-builder-race:${index}] ${chunk}`);
    });
    child.on("exit", (code, signal) => {
      if (shuttingDown) return;
      process.stderr.write(`[e4-builder-race:${index}] exited code=${code} signal=${signal || ""}; restarting\n`);
      const queue = generationsByChild.get(index) || [];
      while (queue.length) {
        const id = queue.shift();
        settle(id, JSON.stringify({ok: false, error: `builder child ${index} exited`}), false);
      }
      setTimeout(() => startChild(index), 50).unref();
    });
  }

  for (let index = 0; index < childCount; index += 1) startChild(index);

  const input = createInterface({input: process.stdin, crlfDelay: Infinity});
  input.on("line", (line) => {
    if (!line.trim()) return;
    generation += 1;
    const id = generation;
    const item = {
      responses: [],
      emitted: false,
      timer: setTimeout(() => {
        if (item.emitted) return;
        item.emitted = true;
        process.stdout.write(JSON.stringify({ok: false, error: "E4 builder race timed out"}) + "\n");
        pending.delete(id);
      }, Math.max(250, Number(process.env.E4_BUILDER_RACE_TIMEOUT_MS || 2_000))),
    };
    item.timer.unref();
    pending.set(id, item);
    children.forEach((child, index) => {
      const queue = generationsByChild.get(index) || [];
      queue.push(id);
      generationsByChild.set(index, queue);
      if (child?.stdin?.writable) child.stdin.write(line.endsWith("\n") ? line : `${line}\n`);
      else settle(id, JSON.stringify({ok: false, error: `builder child ${index} unavailable`}), false);
    });
  });

  function shutdown(signalName) {
    if (shuttingDown) return;
    shuttingDown = true;
    input.close();
    for (const child of children) {
      try {
        child?.kill(signalName === "SIGINT" ? "SIGINT" : "SIGTERM");
      } catch {
        // no-op
      }
    }
    setTimeout(() => process.exit(0), 250).unref();
  }

  process.on("SIGTERM", () => shutdown("SIGTERM"));
  process.on("SIGINT", () => shutdown("SIGINT"));
}
