"use strict";
const assert = require("node:assert/strict");
const api = require("./index.js");

assert.equal(api.toBigIntBE(Buffer.from([0x01, 0x00])), 256n);
assert.equal(api.toBigIntLE(Buffer.from([0x00, 0x01])), 256n);
assert.deepEqual([...api.toBufferBE(256n, 2)], [0x01, 0x00]);
assert.deepEqual([...api.toBufferLE(256n, 2)], [0x00, 0x01]);
assert.throws(() => api.toBigIntLE(null), TypeError);
assert.throws(() => api.toBufferBE(256n, 1), RangeError);
assert.throws(() => api.toBufferLE(-1n, 8), RangeError);
process.stdout.write("bigint-buffer compatibility shim: PASS\n");
