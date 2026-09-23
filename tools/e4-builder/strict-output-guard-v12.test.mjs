import assert from "node:assert/strict";
import {protectRequest} from "./strict-output-guard-v12.mjs";

const base = {
  request_id: "guard-self-test",
  side: "BUY",
  amount: 0.30,
  slippage_bps: 9000,
  metadata: {
    strict_output_guard: true,
    max_output_shortfall_bps: 800,
    virtual_sol_reserves: 30_000_000_000,
    virtual_token_reserves: 1_073_000_000_000_000,
    real_token_reserves: 793_100_000_000_000,
    total_fee_bps: 125,
  },
};

const quoted = protectRequest({...base, metadata: {...base.metadata, expected_token_output: 1}});
assert.equal(quoted.slippage_bps, 800);
assert.equal(quoted.metadata.strict_output_guard_applied, true);
assert.ok(quoted.metadata.current_quoted_token_output > 0);

assert.throws(
  () => protectRequest({
    ...base,
    metadata: {...base.metadata, expected_token_output: 1e12},
  }),
  /strict token-output rejection/,
);

const sell = protectRequest({side: "SELL", amount: 1, metadata: {strict_output_guard: true}});
assert.equal(sell.side, "SELL");

process.stdout.write("strict-output-guard-v12: PASS\n");
