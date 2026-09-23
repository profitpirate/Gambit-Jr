export const DEFAULT_GUARD_BPS = Math.max(
  0,
  Math.min(2_500, Number(process.env.E4_V12_MAX_OUTPUT_SHORTFALL_BPS || 800)),
);
export const DEFAULT_SLIPPAGE_BPS = Math.max(
  0,
  Math.min(2_500, Number(process.env.E4_V12_BUY_SLIPPAGE_BPS || 800)),
);

function finite(value, fallback = 0) {
  const number = Number(value);
  return Number.isFinite(number) ? number : fallback;
}

function normalSol(value) {
  const amount = finite(value);
  return amount >= 1_000_000 ? amount / 1_000_000_000 : amount;
}

function normalTokens(value) {
  const amount = finite(value);
  return amount >= 10_000_000_000 ? amount / 1_000_000 : amount;
}

export function currentQuote(request) {
  const metadata = request.metadata || {};
  const virtualSol = normalSol(metadata.virtual_sol_reserves);
  const virtualTokens = normalTokens(metadata.virtual_token_reserves);
  const realTokens = normalTokens(metadata.real_token_reserves);
  const maxCost = Math.max(0, finite(request.amount));
  const explicitFee = finite(metadata.total_fee_bps ?? metadata.fee_bps, NaN);
  const feeBps = Number.isFinite(explicitFee)
    ? Math.max(0, explicitFee)
    : Math.max(0, finite(metadata.protocol_fee_bps, 100))
      + Math.max(0, finite(metadata.creator_fee_bps, 25));
  if (!(virtualSol > 0 && virtualTokens > 0 && maxCost > 0)) return 0;
  const curveInput = maxCost / (1 + feeBps / 10_000);
  let tokens = curveInput * virtualTokens / (virtualSol + curveInput);
  if (realTokens > 0) tokens = Math.min(tokens, realTokens);
  return Math.max(0, tokens);
}

export function protectRequest(request) {
  if (String(request.side || "").toUpperCase() !== "BUY") return request;
  const metadata = request.metadata || {};
  if (!metadata.strict_output_guard) return request;

  const guardBps = Math.max(
    0,
    Math.min(2_500, finite(metadata.max_output_shortfall_bps, DEFAULT_GUARD_BPS)),
  );
  const expected = Math.max(0, finite(metadata.expected_token_output));
  const quoted = currentQuote(request);
  if (expected > 0) {
    const minimum = expected * (1 - guardBps / 10_000);
    if (!(quoted > 0) || quoted + Math.max(1e-9, minimum * 1e-12) < minimum) {
      const error = new Error(
        `E4 V12 strict token-output rejection quoted=${quoted} required=${minimum} expected=${expected} guard_bps=${guardBps}`,
      );
      error.request_id = request.request_id || null;
      throw error;
    }
  }

  return {
    ...request,
    slippage_bps: Math.min(
      Math.max(0, finite(request.slippage_bps, DEFAULT_SLIPPAGE_BPS)),
      DEFAULT_SLIPPAGE_BPS,
      guardBps,
    ),
    metadata: {
      ...metadata,
      current_quoted_token_output: quoted,
      strict_output_guard_applied: true,
    },
  };
}

export function protectLine(line) {
  const request = JSON.parse(line);
  return JSON.stringify(protectRequest(request));
}
