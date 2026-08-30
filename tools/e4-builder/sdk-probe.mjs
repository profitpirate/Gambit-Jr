#!/usr/bin/env node
import fs from "node:fs";

const report = {generated_at: new Date().toISOString(), packages: {}};

async function inspectPackage(name) {
  try {
    const module = await import(name);
    const exported = Object.keys(module).sort();
    const objects = {};
    for (const key of exported) {
      const value = module[key];
      if (value && (typeof value === "object" || typeof value === "function")) {
        let prototype = [];
        try {
          prototype = Object.getOwnPropertyNames(
            typeof value === "function" ? value.prototype || {} : Object.getPrototypeOf(value) || {},
          ).sort();
        } catch {}
        const own = [];
        try { own.push(...Object.getOwnPropertyNames(value).sort()); } catch {}
        if (prototype.length || own.length) objects[key] = {prototype, own};
      }
    }
    report.packages[name] = {ok: true, exported, objects};
  } catch (error) {
    report.packages[name] = {ok: false, error: error?.stack || String(error)};
  }
}

await inspectPackage("@pump-fun/pump-sdk");
await inspectPackage("@pump-fun/pump-swap-sdk");
fs.mkdirSync("outputs", {recursive: true});
fs.writeFileSync("outputs/e4-offline-sdk-probe.json", JSON.stringify(report, null, 2));
console.log(JSON.stringify(report, null, 2));
