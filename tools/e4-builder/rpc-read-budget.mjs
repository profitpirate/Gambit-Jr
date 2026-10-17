// Only the unsigned builder uses this transport. It never sends transactions.
const READ_METHODS = new Set(['getMultipleAccounts', 'getAccountInfo', 'getLatestBlockhash',
  'getBalance', 'getVersion', 'getSlot', 'getTokenAccountBalance', 'getMinimumBalanceForRentExemption']);

export class RpcReadBudget {
  constructor() { this.samples = new Map(); }
  deadline(method) {
    const simulation = method === 'simulateTransaction';
    const [floor, initial, ceiling] = simulation ? [1200, 2500, 2500] : [400, 1000, 1500];
    const samples = this.samples.get(method) || [];
    if (samples.length < 8) return initial;
    const sorted = [...samples].sort((a,b)=>a-b);
    const p95 = sorted[Math.ceil(sorted.length * .95) - 1];
    return Math.ceil(Math.min(ceiling, Math.max(floor, p95 * 3 + 100)));
  }
  observe(method, duration) {
    if (!Number.isFinite(duration) || duration < 0 || duration > 120000) return;
    const samples = this.samples.get(method) || [];
    samples.push(duration);
    if (samples.length > 64) samples.shift();
    this.samples.set(method, samples);
  }
}

export function createReadFetch({fetchImpl = globalThis.fetch, onRateLimit = ()=>{},
  budget = new RpcReadBudget(), clock = ()=>performance.now(), timeoutSignal = ms=>AbortSignal.timeout(ms)} = {}) {
  return async (input, init) => {
    const method = JSON.parse(init.body).method;
    if (!READ_METHODS.has(method) && method !== 'simulateTransaction')
      throw new Error('UNSIGNED_BUILDER_RPC_METHOD_FORBIDDEN');
    const timer = timeoutSignal(budget.deadline(method));
    const signal = init.signal ? AbortSignal.any([init.signal, timer]) : timer;
    const started = clock();
    const response = await fetchImpl(input, {...init, signal});
    if (response.status === 429) onRateLimit(response.headers.get('retry-after'));
    // Read the complete body under the same deadline. Header-only timing hides
    // stalled bodies and would train an incorrectly short deadline.
    const body = await response.text();
    if (response.status === 200) {
      try {
        const value = JSON.parse(body);
        if (!value.error && Object.hasOwn(value, 'result')) budget.observe(method, clock()-started);
      } catch {} // web3.js retains responsibility for rejecting malformed JSON.
    }
    return new Response(body, {status:response.status, statusText:response.statusText, headers:response.headers});
  };
}
