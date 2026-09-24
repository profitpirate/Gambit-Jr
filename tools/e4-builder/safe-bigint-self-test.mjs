import assert from "node:assert/strict";
import {createRequire} from "node:module";

const require = createRequire(import.meta.url);
const safe = require("bigint-buffer");

assert.equal(safe.toBigIntBE(Buffer.from("deadbeef", "hex")), 0xdeadbeefn);
assert.equal(safe.toBigIntLE(Buffer.from("efbeadde", "hex")), 0xdeadbeefn);
assert.equal(safe.toBufferBE(0xdeadbeefn, 8).toString("hex"), "00000000deadbeef");
assert.equal(safe.toBufferLE(0xdeadbeefn, 8).toString("hex"), "efbeadde00000000");
assert.throws(() => safe.toBigIntLE(null), TypeError);
assert.throws(() => safe.toBufferBE(-1n, 8), TypeError);
assert.throws(() => safe.toBufferBE(256n, 1), RangeError);

const packageJson = require("bigint-buffer/package.json");
assert.equal(packageJson.version, "1.1.6");
console.log(JSON.stringify({ok: true, implementation: "vendored-pure-js", version: packageJson.version}));
