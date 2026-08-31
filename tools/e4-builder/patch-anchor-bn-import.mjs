#!/usr/bin/env node
import {access, readdir, readFile, writeFile} from "node:fs/promises";
import path from "node:path";
import {fileURLToPath} from "node:url";

const root = path.join(
  path.dirname(fileURLToPath(import.meta.url)),
  "node_modules",
  "@pump-fun",
  "agent-payments-sdk",
  "dist",
);

const brokenBnImport = /import\s*\{\s*BN\s+as\s+([A-Za-z_$][\w$]*)\s*\}\s*from\s*["']@coral-xyz\/anchor["'];?/g;

async function filesUnder(directory) {
  const output = [];
  for (const entry of await readdir(directory, {withFileTypes: true})) {
    const target = path.join(directory, entry.name);
    if (entry.isDirectory()) output.push(...await filesUnder(target));
    else if (/\.(?:m?js|cjs)$/.test(entry.name)) output.push(target);
  }
  return output;
}

try {
  await access(root);
} catch {
  process.stdout.write("E4 Pump SDK interop patch skipped: agent-payments SDK not installed\n");
  process.exit(0);
}

let patched = 0;
let remainingBrokenImports = 0;
for (const file of await filesUnder(root)) {
  const original = await readFile(file, "utf8");
  const replacement = original.replace(
    brokenBnImport,
    (_match, localName) => `import anchorPackage from "@coral-xyz/anchor";const {BN:${localName}}=anchorPackage;`,
  );
  if (replacement !== original) {
    await writeFile(file, replacement, "utf8");
    patched += 1;
  }
  brokenBnImport.lastIndex = 0;
  if (brokenBnImport.test(replacement)) remainingBrokenImports += 1;
  brokenBnImport.lastIndex = 0;
}

if (remainingBrokenImports) {
  throw new Error(`E4 Pump SDK interop patch left ${remainingBrokenImports} invalid BN imports`);
}
process.stdout.write(`E4 Pump SDK interop patch complete: ${patched} file(s) updated\n`);
