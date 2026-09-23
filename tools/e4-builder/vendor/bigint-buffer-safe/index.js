"use strict";

function assertBuffer(value, name) {
  if (!Buffer.isBuffer(value) && !(value instanceof Uint8Array)) {
    throw new TypeError(`${name}: expected Buffer or Uint8Array`);
  }
  return Buffer.from(value);
}

function assertWidth(width) {
  if (!Number.isSafeInteger(width) || width < 0) {
    throw new TypeError("width must be a non-negative safe integer");
  }
}

function assertBigInt(value, name) {
  if (typeof value !== "bigint") {
    throw new TypeError(`${name}: expected bigint`);
  }
  if (value < 0n) throw new RangeError(`${name}: negative values are unsupported`);
}

function toBigIntBE(input) {
  const buffer = assertBuffer(input, "toBigIntBE");
  let value = 0n;
  for (const byte of buffer) value = (value << 8n) | BigInt(byte);
  return value;
}

function toBigIntLE(input) {
  const buffer = assertBuffer(input, "toBigIntLE");
  let value = 0n;
  for (let index = buffer.length - 1; index >= 0; index -= 1) {
    value = (value << 8n) | BigInt(buffer[index]);
  }
  return value;
}

function toBufferBE(value, width) {
  assertBigInt(value, "toBufferBE");
  assertWidth(width);
  const out = Buffer.alloc(width);
  let remaining = value;
  for (let index = width - 1; index >= 0; index -= 1) {
    out[index] = Number(remaining & 0xffn);
    remaining >>= 8n;
  }
  if (remaining !== 0n) throw new RangeError("toBufferBE: bigint does not fit requested width");
  return out;
}

function toBufferLE(value, width) {
  assertBigInt(value, "toBufferLE");
  assertWidth(width);
  const out = Buffer.alloc(width);
  let remaining = value;
  for (let index = 0; index < width; index += 1) {
    out[index] = Number(remaining & 0xffn);
    remaining >>= 8n;
  }
  if (remaining !== 0n) throw new RangeError("toBufferLE: bigint does not fit requested width");
  return out;
}

module.exports = {toBigIntBE, toBigIntLE, toBufferBE, toBufferLE};
