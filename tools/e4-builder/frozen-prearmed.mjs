// Dedicated unsigned-transaction builder. No keys, signing, broadcasting or remote builder.
import readline from 'node:readline';
import {pathToFileURL} from 'node:url';
import BN from 'bn.js';
import {Connection, PublicKey, ComputeBudgetProgram, TransactionMessage, VersionedTransaction} from '@solana/web3.js';
import {NATIVE_MINT, TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID, unpackMint} from '@solana/spl-token';
import {PUMP_SDK, OnlinePumpSdk, PUMP_PROGRAM_ID, pumpIdl,
  getBuyTokenAmountFromSolAmount, getSellSolAmountFromTokenAmount} from '@pump-fun/pump-sdk';
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

export function guards({expected, current, price, launchPrice}) {
  if (!(Number.isFinite(price) && price > 0 && Number.isFinite(launchPrice) && launchPrice > 0))
    throw new Error('invalid curve reference price');
  if (price > launchPrice * 1.5) throw new Error('ENTRY_CHASE_GUARD_REJECTED');
  const minimum = (expected * 65n + 99n) / 100n;
  if (current < minimum || minimum <= 0n) throw new Error('ENTRY_OUTPUT_GUARD_REJECTED');
  return minimum;
}

export async function handle(request, connection) {
  const online = new OnlinePumpSdk(connection);
  if (request.action === 'HEALTH') {
    const [global, fees, version, slot] = await Promise.all([
      online.fetchGlobal(), online.fetchFeeConfig(), connection.getVersion(), connection.getSlot('confirmed')]);
    if (!global || !fees) throw new Error('Pump configuration unavailable');
    return {ok: true, slot, solana_core: version['solana-core'], builder: 'frozen-prearmed-sdk-2.0.0'};
  }
  const mint = new PublicKey(request.mint), user = new PublicKey(request.public_key);
  const mintInfo = await connection.getAccountInfo(mint, 'confirmed');
  if (!mintInfo || ![TOKEN_PROGRAM_ID.toBase58(), TOKEN_2022_PROGRAM_ID.toBase58()].includes(mintInfo.owner.toBase58()))
    throw new Error('unsupported token program');
  const tokenProgram = mintInfo.owner;
  const mintState = unpackMint(mint, mintInfo, tokenProgram);
  if (mintState.decimals !== 6) throw new Error('frozen model requires six token decimals');
  const [global, feeConfig, state] = await Promise.all([
    online.fetchGlobal(), online.fetchFeeConfig(), online.fetchBuyState(mint, user, tokenProgram)]);
  const curve = state.bondingCurve;
  if (curve.complete && request.side !== 'SELL') throw new Error('CURVE_MIGRATED');
  if (!state.quoteMint.equals(NATIVE_MINT)) throw new Error('only SOL quote curves are supported');
  if (curve.isMayhemMode) throw new Error('mayhem curves are excluded');
  if (!feeConfig) throw new Error('dynamic fee configuration unavailable');
  if (request.side === 'BUY' && request.creator && curve.creator.toBase58() !== request.creator) throw new Error('creator changed');
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
    if (amount <= 0n) throw new Error('budget does not cover transaction costs');
    quoted = BigInt(getBuyTokenAmountFromSolAmount({...base, amount: new BN(amount.toString()), quoteMint: NATIVE_MINT}).toString());
    const expected = request.expected_tokens_raw ? integer(request.expected_tokens_raw, 'expected tokens') : quoted;
    minimum = guards({expected, current: quoted, price, launchPrice: Number(request.launch_price)});
    if (request.action === 'QUOTE') return {quoted_tokens_raw: quoted.toString(), curve_price: price,
      spend_lamports: amount.toString(), budget_lamports: budget.toString(), token_program: tokenProgram.toBase58()};
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
  const {blockhash, lastValidBlockHeight} = await connection.getLatestBlockhash('confirmed');
  const message = new TransactionMessage({payerKey: user, recentBlockhash: blockhash, instructions}).compileToV0Message();
  const transaction = new VersionedTransaction(message);
  const bytes = transaction.serialize();
  if (bytes.length > 1232) throw new Error('transaction exceeds supported packet size');
  const simulation = await connection.simulateTransaction(transaction, {
    sigVerify: false, commitment: 'confirmed', accounts: {encoding: 'base64', addresses: [user.toBase58()]}});
  if (simulation.value.err) throw new Error('SIMULATION_FAILED');
  const before = await connection.getBalance(user, 'confirmed');
  const after = simulation.value.accounts?.[0]?.lamports;
  if (!Number.isSafeInteger(after)) throw new Error('simulation missing wallet balance');
  if (budget && BigInt(Math.max(0, before - after)) > budget) throw new Error('SIMULATION_BUDGET_EXCEEDED');
  return {transaction_base64: Buffer.from(bytes).toString('base64'), curve_price: price,
    last_valid_block_height: lastValidBlockHeight, blockhash,
    quoted_raw: quoted.toString(), minimum_tokens_raw: minimum?.toString(),
    simulation_passed: true, simulation_slot: simulation.context.slot};
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const endpoints = [...new Set([process.env.E4_PRIMARY_RPC_URL,
    ...(process.env.E4_FALLBACK_RPC_URLS || '').split(',')].filter(Boolean))];
  if (endpoints.length < 2 || endpoints.some(e => !e.startsWith('https://'))) throw new Error('Two TLS RPC endpoints required');
  const connections = endpoints.map(endpoint => new Connection(endpoint, {commitment: 'confirmed',
    disableRetryOnRateLimit: true,
    fetch: (input, init) => fetch(input, {...init, signal: AbortSignal.timeout(2500)})}));
  for await (const line of readline.createInterface({input: process.stdin, crlfDelay: Infinity})) {
    let request;
    try {
      request = JSON.parse(line);
      let result, lastError;
      for (const connection of connections) {
        try { result = await handle(request, connection); break; }
        catch (error) { lastError = error; }
      }
      if (!result) throw lastError;
      process.stdout.write(JSON.stringify({request_id: request.request_id, result}) + '\n');
    } catch (error) {
      // RPC errors can contain provider credentials; never echo their messages.
      const reason = /^(CURVE_MIGRATED|ENTRY_\w+|SIMULATION_\w+)$/.test(error.message) ? error.message : 'BUILD_REJECTED';
      process.stdout.write(JSON.stringify({request_id: request?.request_id, error: reason}) + '\n');
    }
  }
}
