// Dedicated unsigned-transaction builder. No keys, signing, broadcasting or remote builder.
import readline from 'node:readline';
import {pathToFileURL} from 'node:url';
import {createReadFetch} from './rpc-read-budget.mjs';
import BN from 'bn.js';
import {Connection, PublicKey, ComputeBudgetProgram, TransactionMessage, VersionedTransaction} from '@solana/web3.js';
import {NATIVE_MINT, TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID, unpackMint, getAssociatedTokenAddressSync} from '@solana/spl-token';
import {PUMP_SDK, OnlinePumpSdk, PUMP_PROGRAM_ID, PUMP_FEE_PROGRAM_ID, feeSharingConfigPda, pumpIdl,
  getBuyTokenAmountFromSolAmount, getSellSolAmountFromTokenAmount,
  GLOBAL_PDA, PUMP_FEE_CONFIG_PDA, bondingCurvePda} from '@pump-fun/pump-sdk';
import {OnlinePumpAmmSdk, PUMP_AMM_SDK, canonicalPumpPoolPda, sellBaseInput} from '@pump-fun/pump-swap-sdk';

export async function migratedSell(connection, mint, user, amount) {
  const state = await new OnlinePumpAmmSdk(connection).swapSolanaState(canonicalPumpPoolPda(mint), user);
  if (!state.pool.baseMint.equals(mint) || !state.pool.quoteMint.equals(NATIVE_MINT)
      || state.pool.isMayhemMode || !state.feeConfig) throw new Error('unsupported migrated pool');
  const quote = sellBaseInput({base: new BN(amount.toString()), slippage: 10,
    baseReserve: state.poolBaseAmount, quoteReserve: state.poolQuoteAmount,
    virtualQuoteReserves: state.pool.virtualQuoteReserves, globalConfig: state.globalConfig,
    baseMintAccount: state.baseMintAccount, baseMint: mint, coinCreator: state.pool.coinCreator,
    creator: state.pool.creator, feeConfig: state.feeConfig, quoteMint: NATIVE_MINT,
    isMayhemMode: false, creatorFeeBps: state.pool.creatorFeeBps});
  if (quote.minQuote.lte(new BN(0))) throw new Error('zero migrated sell output');
  return {instructions: await PUMP_AMM_SDK.sellInstructions(state, new BN(amount.toString()), quote.minQuote),
    quoted: BigInt(quote.minQuote.toString())};
}

export function integer(value, name) {
  if (typeof value !== 'string' || !/^[0-9]+$/.test(value)) throw new Error(`invalid ${name}`);
  const result = BigInt(value);
  if (result <= 0n || result > 18_446_744_073_709_551_615n) throw new Error(`invalid ${name}`);
  return result;
}

export function exactQuoteInstruction(instruction, spend, minimum) {
  const buy = pumpIdl.instructions.find(i => i.name === 'buy_v2');
  const exact = pumpIdl.instructions.find(i => i.name === 'buy_exact_quote_in_v2');
  const accounts = i => i.accounts.map(a => [a.name, !!a.writable, !!a.signer]);
  if (!buy || !exact || JSON.stringify(accounts(buy)) !== JSON.stringify(accounts(exact))
      || !instruction.programId.equals(PUMP_PROGRAM_ID)
      || !instruction.data.subarray(0, 8).equals(Buffer.from(buy.discriminator))
      || instruction.keys.length !== buy.accounts.length) throw new Error('SDK buy ABI changed');
  const data = Buffer.alloc(24);
  Buffer.from(exact.discriminator).copy(data);
  data.writeBigUInt64LE(spend, 8);
  data.writeBigUInt64LE(minimum, 16);
  instruction.data = data;
  return instruction;
}

export function guards({expected, current, price, launchPrice, stateSlot}) {
  if (!(Number.isFinite(price) && price > 0 && Number.isFinite(launchPrice) && launchPrice > 0))
    throw new Error('invalid curve reference price');
  if (price > launchPrice * 1.5) {
    const error = new Error('ENTRY_CHASE_GUARD_REJECTED');
    error.quote_facts = {launch_price: launchPrice, curve_price: price,
      price_multiple: price / launchPrice, maximum_price_multiple: 1.5, state_slot: stateSlot};
    throw error;
  }
  const minimum = (expected * 65n + 99n) / 100n;
  if (current < minimum || minimum <= 0n) throw new Error('ENTRY_OUTPUT_GUARD_REJECTED');
  return minimum;
}

export async function verifyBuyCreator(connection, mint, curveCreator, expectedCreator, proofAccount = undefined) {
  let expected;
  try { expected = new PublicKey(expectedCreator); }
  catch { throw new Error('QUOTE_CREATOR_REQUIRED'); }
  if (curveCreator.equals(expected)) return {creator_verification: 'DIRECT_WALLET', curve_creator: curveCreator.toBase58()};
  // Pump fee sharing deliberately replaces the curve's creator with this mint's
  // canonical config PDA. Resolve its authority rather than comparing the PDA
  // with a wallet. A different wallet or unrelated PDA remains rejected.
  const sharingAddress = feeSharingConfigPda(mint);
  if (!curveCreator.equals(sharingAddress)) throw new Error('QUOTE_CREATOR_CHANGED');
  const account = proofAccount === undefined
    ? await connection.getAccountInfo(sharingAddress, 'confirmed')
      .catch(() => { throw new Error('QUOTE_CREATOR_PROOF_UNAVAILABLE'); })
    : proofAccount;
  if (!account) throw new Error('QUOTE_CREATOR_PROOF_UNAVAILABLE');
  if (!account.owner.equals(PUMP_FEE_PROGRAM_ID) || account.executable)
    throw new Error('QUOTE_CREATOR_PROOF_INVALID');
  let sharing;
  try { sharing = PUMP_SDK.decodeSharingConfig(account); }
  catch { throw new Error('QUOTE_CREATOR_PROOF_INVALID'); }
  if (!sharing.mint.equals(mint)) throw new Error('QUOTE_CREATOR_PROOF_INVALID');
  if (!sharing.admin.equals(expected)) throw new Error('QUOTE_CREATOR_CHANGED');
  // adminRevoked locks future share edits; it does not erase the admin identity.
  return {creator_verification: 'FEE_SHARING_AUTHORITY', curve_creator: curveCreator.toBase58(),
    fee_sharing_admin: sharing.admin.toBase58(), fee_sharing_admin_revoked: sharing.adminRevoked};
}

export async function readSnapshot(connection, mint, user) {
  // One current bank snapshot replaces sequential mint, global, fee, curve,
  // ATA and creator-proof reads. Neither quotes nor mutable fees are cached.
  let response;
  try {
    response = await connection.getMultipleAccountsInfoAndContext([
      mint, GLOBAL_PDA, PUMP_FEE_CONFIG_PDA, bondingCurvePda(mint), feeSharingConfigPda(mint),
      getAssociatedTokenAddressSync(mint, user, true, TOKEN_PROGRAM_ID),
      getAssociatedTokenAddressSync(mint, user, true, TOKEN_2022_PROGRAM_ID),
    ], 'confirmed');
  } catch { throw new Error('QUOTE_STATE_UNAVAILABLE'); }
  if (!Number.isSafeInteger(response?.context?.slot) || response.context.slot <= 0
      || !Array.isArray(response.value) || response.value.length !== 7) throw new Error('QUOTE_STATE_UNAVAILABLE');
  const [mintInfo, globalInfo, feeInfo, curveInfo, sharingInfo, legacyAta, token2022Ata] = response.value;
  if (!mintInfo) throw new Error('QUOTE_MINT_UNAVAILABLE');
  if (mintInfo.executable || ![TOKEN_PROGRAM_ID.toBase58(), TOKEN_2022_PROGRAM_ID.toBase58()].includes(mintInfo.owner.toBase58()))
    throw new Error('QUOTE_TOKEN_UNSUPPORTED');
  const tokenProgram = mintInfo.owner;
  const mintState = unpackMint(mint, mintInfo, tokenProgram);
  if (mintState.decimals !== 6) throw new Error('QUOTE_DECIMALS_UNSUPPORTED');
  if (!globalInfo || !curveInfo || globalInfo.executable || curveInfo.executable
      || !globalInfo.owner.equals(PUMP_PROGRAM_ID) || !curveInfo.owner.equals(PUMP_PROGRAM_ID))
    throw new Error('QUOTE_STATE_UNAVAILABLE');
  if (!feeInfo || feeInfo.executable || !feeInfo.owner.equals(PUMP_FEE_PROGRAM_ID)) throw new Error('QUOTE_FEES_UNAVAILABLE');
  let global, feeConfig, curve;
  try {
    global = PUMP_SDK.decodeGlobal(globalInfo);
    feeConfig = PUMP_SDK.decodeFeeConfig(feeInfo);
    curve = PUMP_SDK.decodeBondingCurve(curveInfo);
  } catch { throw new Error('QUOTE_STATE_UNAVAILABLE'); }
  if (!curve.quoteMint.equals(PublicKey.default) && !curve.quoteMint.equals(NATIVE_MINT)) throw new Error('QUOTE_CURRENCY_UNSUPPORTED');
  return {global, feeConfig, mintState, tokenProgram, sharingInfo, stateSlot: response.context.slot,
    state: {bondingCurve: curve, bondingCurveAccountInfo: curveInfo,
      associatedUserAccountInfo: tokenProgram.equals(TOKEN_PROGRAM_ID) ? legacyAta : token2022Ata,
      quoteMint: NATIVE_MINT, quoteTokenProgram: TOKEN_PROGRAM_ID}};
}

async function build(request, connection, timings) {
  if (request.action === 'WARMUP') return {ok: true, builder: 'frozen-prearmed-sdk-2.0.0'};
  if (request.action === 'HEALTH') {
    // OnlinePumpSdk reconstructs and camel-cases the complete Anchor IDL. It is
    // used only by this startup probe; quotes/builds use the existing PUMP_SDK.
    // Constructing it on every quote wasted ~30 ms without performing a read.
    const online = new OnlinePumpSdk(connection);
    const [global, fees, version, slot] = await Promise.all([
      online.fetchGlobal(), online.fetchFeeConfig(), connection.getVersion(), connection.getSlot('confirmed')]);
    if (!global || !fees) throw new Error('Pump configuration unavailable');
    return {ok: true, slot, solana_core: version['solana-core'], builder: 'frozen-prearmed-sdk-2.0.0'};
  }
  const mint = new PublicKey(request.mint), user = new PublicKey(request.public_key);
  // Read-only speculation: no signed bytes or order exist here. Keep the
  // outcome captured so an early policy rejection cannot leak a rejected task.
  const blockhashStarted = performance.now();
  const blockhashRead = request.action === 'BUILD'
    ? connection.getLatestBlockhashAndContext({commitment:'confirmed'})
      .then(value=>({value, finished:performance.now()}), error=>({error})) : null;
  const readStarted = performance.now();
  let snapshot;
  try { snapshot = await readSnapshot(connection, mint, user); }
  finally { timings.state_read_ms = performance.now() - readStarted; }
  const {global, feeConfig, mintState, tokenProgram, state, sharingInfo, stateSlot} = snapshot;
  const curve = state.bondingCurve;
  if (curve.complete && request.side !== 'SELL') throw new Error('CURVE_MIGRATED');
  if (!state.quoteMint.equals(NATIVE_MINT)) throw new Error('QUOTE_CURRENCY_UNSUPPORTED');
  if (curve.isMayhemMode) throw new Error('QUOTE_MAYHEM');
  if (!feeConfig) throw new Error('QUOTE_FEES_UNAVAILABLE');
  const creatorProof = request.side === 'BUY'
    ? await verifyBuyCreator(connection, mint, curve.creator, request.creator, sharingInfo) : {};
  const price = Number(curve.virtualQuoteReserves.toString()) / Number(curve.virtualTokenReserves.toString()) / 1000;
  const base = {global, feeConfig, mintSupply: new BN(mintState.supply.toString()), bondingCurve: curve};
  const priority = 1_000_000n, feeReserve = 5_000n;
  // Reserve the actual token-account size plus room for Pump account creation.
  // Simulation below must independently prove the total wallet debit fits the budget.
  const rentReserve = 5_000_000n;
  let instructions, amount, quoted, minimum, budget;
  if (request.side === 'BUY') {
    budget = integer(request.budget_lamports, 'budget');
    amount = budget - priority - feeReserve - rentReserve;
    if (amount <= 0n) throw new Error('QUOTE_BUDGET_INSUFFICIENT');
    quoted = BigInt(getBuyTokenAmountFromSolAmount({...base, amount: new BN(amount.toString()), quoteMint: NATIVE_MINT}).toString());
    const expected = request.expected_tokens_raw ? integer(request.expected_tokens_raw, 'expected tokens') : quoted;
    minimum = guards({expected, current: quoted, price, launchPrice: Number(request.launch_price), stateSlot});
    if (request.action === 'QUOTE') return {quoted_tokens_raw: quoted.toString(), curve_price: price,
      spend_lamports: amount.toString(), budget_lamports: budget.toString(), token_program: tokenProgram.toBase58(), state_slot: stateSlot, ...creatorProof};
    instructions = await PUMP_SDK.buyV2Instructions({...state, global, mint, user, tokenProgram,
      amount: new BN(quoted.toString()), quoteAmount: new BN(amount.toString()), slippage: 0});
    const swaps = instructions.filter(i => i.programId.equals(PUMP_PROGRAM_ID));
    if (swaps.length !== 1) throw new Error('unexpected Pump buy instruction count');
    exactQuoteInstruction(swaps[0], amount, minimum);
  } else if (request.side === 'SELL') {
    amount = integer(request.tokens_raw, 'tokens');
    if (curve.complete) {
      ({instructions, quoted} = await migratedSell(connection, mint, user, amount));
    } else {
    quoted = BigInt(getSellSolAmountFromTokenAmount({...base, amount: new BN(amount.toString())}).toString());
    if (quoted <= 0n) throw new Error('sell quote is zero');
    instructions = await PUMP_SDK.sellV2Instructions({...state, global, mint, user, tokenProgram,
      amount: new BN(amount.toString()), quoteAmount: new BN(quoted.toString()), slippage: 10});
    }
  } else throw new Error('unsupported side');
  if (request.action !== 'BUILD') throw new Error('unsupported action');
  const units = 400_000;
  instructions.unshift(ComputeBudgetProgram.setComputeUnitLimit({units}),
    ComputeBudgetProgram.setComputeUnitPrice({microLamports: priority * 1_000_000n / BigInt(units)}));
  let blockhashResult = await blockhashRead;
  if (blockhashResult.error) throw blockhashResult.error;
  // A prefetched blockhash must come from a bank at least as recent as the
  // account snapshot. If the bank advanced, obtain it again at that context.
  if (!Number.isSafeInteger(blockhashResult.value?.context?.slot)
      || blockhashResult.value.context.slot < stateSlot
      || performance.now()-blockhashResult.finished > 1000) {
    blockhashResult = {value:await connection.getLatestBlockhashAndContext({commitment:'confirmed', minContextSlot:stateSlot})};
  }
  if (!Number.isSafeInteger(blockhashResult.value?.context?.slot) || blockhashResult.value.context.slot < stateSlot)
    throw new Error('BLOCKHASH_CONTEXT_STALE');
  const {blockhash, lastValidBlockHeight} = blockhashResult.value.value;
  if (typeof blockhash !== 'string' || !Number.isSafeInteger(lastValidBlockHeight) || lastValidBlockHeight <= 0)
    throw new Error('BLOCKHASH_RESPONSE_INVALID');
  timings.blockhash_ready_ms = performance.now()-blockhashStarted;
  const message = new TransactionMessage({payerKey: user, recentBlockhash: blockhash, instructions}).compileToV0Message();
  const transaction = new VersionedTransaction(message);
  const bytes = transaction.serialize();
  if (bytes.length > 1232) throw new Error('transaction exceeds supported packet size');
  const simulationStarted = performance.now();
  const simulation = await connection.simulateTransaction(transaction, {
    sigVerify: false, commitment: 'confirmed', accounts: {encoding: 'base64', addresses: [user.toBase58()]}});
  timings.simulation_ms = performance.now() - simulationStarted;
  if (simulation.value.err) throw new Error('SIMULATION_FAILED');
  const balanceStarted = performance.now();
  const before = await connection.getBalance(user, 'confirmed');
  timings.balance_read_ms = performance.now()-balanceStarted;
  const after = simulation.value.accounts?.[0]?.lamports;
  if (!Number.isSafeInteger(after)) throw new Error('simulation missing wallet balance');
  if (budget && BigInt(Math.max(0, before - after)) > budget) throw new Error('SIMULATION_BUDGET_EXCEEDED');
  if (request.maximum_debit_lamports && BigInt(Math.max(0, before - after)) > integer(request.maximum_debit_lamports, 'maximum debit'))
    throw new Error('SIMULATION_BUDGET_EXCEEDED');
  return {transaction_base64: Buffer.from(bytes).toString('base64'), curve_price: price,
    last_valid_block_height: lastValidBlockHeight, blockhash,
    quoted_raw: quoted.toString(), minimum_tokens_raw: minimum?.toString(),
    simulation_passed: true, simulation_slot: simulation.context.slot, state_slot: stateSlot, ...creatorProof};
}

export async function handle(request, connection) {
  const started = performance.now(), timings = {};
  try {
    const result = await build(request, connection, timings);
    return {...result, builder_timings: {...timings, total_ms: performance.now() - started}};
  } catch (error) {
    error.builder_timings = {...timings, total_ms: performance.now() - started};
    throw error;
  }
}

export function retryableBuilderError(error) {
  // A different RPC is useful for transport/unavailable-state failures. A
  // definitive policy rejection must not run the same checks on every RPC.
  return !/^(CURVE_MIGRATED|ENTRY_\w+|QUOTE_(TOKEN_UNSUPPORTED|DECIMALS_UNSUPPORTED|CURRENCY_UNSUPPORTED|MAYHEM|CREATOR_CHANGED|CREATOR_REQUIRED|CREATOR_PROOF_INVALID|BUDGET_INSUFFICIENT))$/.test(error?.message || '');
}

export function rpcCooldownMs(header, now = Date.now()) {
  const seconds = typeof header === 'string' && /^\d+(?:\.\d+)?$/.test(header.trim())
    ? Number(header) : (Date.parse(header) - now) / 1000;
  return Math.min(3600000, Math.max(1000, Number.isFinite(seconds) ? seconds * 1000 : 60000));
}

export async function routeRequest(request, connections, cooldowns, now = Date.now) {
  let lastError;
  const started=performance.now();
  let attempted=0;
  for (const [providerIndex, connection] of connections.entries()) {
    if (now() < (cooldowns[providerIndex] || 0)) continue;
    try {
      attempted++;
      const result=await handle(request, connection);
      return {...result, provider_index:providerIndex, builder_timings:{...result.builder_timings,
        rpc_route_ms:performance.now()-started, rpc_failovers:attempted-1}};
    }
    catch (error) {
      lastError = error;
      error.provider_index = providerIndex;
      if (error.code === -32003) cooldowns[providerIndex] = now() + 60000;
      if (!retryableBuilderError(error)) break;
    }
  }
  throw lastError || new Error('QUOTE_RPC_COOLDOWN');
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const endpoints = [...new Set([process.env.E4_PRIMARY_RPC_URL,
    ...(process.env.E4_FALLBACK_RPC_URLS || '').split(',')].filter(Boolean))];
  if (endpoints.length < 2 || endpoints.some(e => !e.startsWith('https://'))) throw new Error('Two TLS RPC endpoints required');
  const cooldowns = [];
  const connections = endpoints.map((endpoint, index) => new Connection(endpoint, {commitment: 'confirmed',
    disableRetryOnRateLimit: true,
    fetch: createReadFetch({onRateLimit:header=>{cooldowns[index]=Date.now()+rpcCooldownMs(header);}})}));
  for await (const line of readline.createInterface({input: process.stdin, crlfDelay: Infinity})) {
    let request;
    try {
      request = JSON.parse(line);
      const result = await routeRequest(request, connections, cooldowns);
      process.stdout.write(JSON.stringify({request_id: request.request_id, result}) + '\n');
    } catch (error) {
      // RPC errors can contain provider credentials; never echo their messages.
      const reason = /^(CURVE_MIGRATED|ENTRY_\w+|SIMULATION_\w+|QUOTE_(RPC_COOLDOWN|MINT_UNAVAILABLE|STATE_UNAVAILABLE|TOKEN_UNSUPPORTED|DECIMALS_UNSUPPORTED|CURRENCY_UNSUPPORTED|MAYHEM|FEES_UNAVAILABLE|CREATOR_CHANGED|CREATOR_REQUIRED|CREATOR_PROOF_UNAVAILABLE|CREATOR_PROOF_INVALID|BUDGET_INSUFFICIENT))$/.test(error.message) ? error.message : 'BUILD_REJECTED';
      const sourceLocation = error.stack?.match(/frozen-prearmed\.mjs:\d+:\d+/)?.[0];
      const errorType = /^[A-Za-z]{1,64}$/.test(error.name) ? error.name : 'Error';
      process.stdout.write(JSON.stringify({request_id: request?.request_id, error: reason,
        error_type: errorType, source_location: sourceLocation,
        builder_timings: error.builder_timings, provider_index: error.provider_index,
        quote_facts: error.quote_facts}) + '\n');
    }
  }
}
