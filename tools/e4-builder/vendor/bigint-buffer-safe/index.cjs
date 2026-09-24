"use strict";

function assertBuffer(value, name) {
  if (!Buffer.isBuffer(value)) {
    throw new TypeError(name + ": expected a Buffer");
  }
}

function assertWidth(width, name) {
  if (!Number.isSafeInteger(width) || width < 0) {
    throw new TypeError(name + ": width must be a non-negative safe integer");
  }
}

function assertBigInt(value, name) {
  if (typeof value !== "bigint" || value < 0n) {
    throw new TypeError(name + ": expected a non-negative bigint");
  }
}

function toBigIntLE(buf) {
  assertBuffer(buf, "toBigIntLE");
  if (buf.length === 0) return 0n;
  const reversed = Buffer.from(buf);
  reversed.reverse();
  return BigInt("0x" + reversed.toString("hex"));
}

function toBigIntBE(buf) {
  assertBuffer(buf, "toBigIntBE");
  if (buf.length === 0) return 0n;
  return BigInt("0x" + buf.toString("hex"));
}

function encoded(num, width, name) {
  assertBigInt(num, name);
  assertWidth(width, name);
  if (width === 0) return Buffer.alloc(0);
  const limit = 1n << BigInt(width * 8);
  if (num >= limit) {
    throw new RangeError(name + ": bigint does not fit requested width");
  }
  return Buffer.from(num.toString(16).padStart(width * 2, "0"), "hex");
}

function toBufferLE(num, width) {
  const value = encoded(num, width, "toBufferLE");
  value.reverse();
  return value;
}

function toBufferBE(num, width) {
  return encoded(num, width, "toBufferBE");
}

module.exports = {toBigIntLE, toBigIntBE, toBufferLE, toBufferBE};
