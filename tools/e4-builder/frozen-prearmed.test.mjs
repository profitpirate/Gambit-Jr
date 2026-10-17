import test from 'node:test';
import assert from 'node:assert/strict';
import BN from 'bn.js';
import {Keypair, PublicKey, VersionedTransaction} from '@solana/web3.js';
import {MintLayout, TOKEN_PROGRAM_ID, NATIVE_MINT} from '@solana/spl-token';
import {OnlinePumpSdk, pumpIdl, PUMP_PROGRAM_ID} from '@pump-fun/pump-sdk';
import {OnlinePumpAmmSdk, PUMP_AMM_PROGRAM_ID, canonicalPumpPoolPda, pumpPoolAuthorityPda} from '@pump-fun/pump-swap-sdk';
import {handle, guards, integer, exactQuoteInstruction} from './frozen-prearmed.mjs';

const pubkey = () => Keypair.generate().publicKey;
const mint = pubkey(), user = pubkey(), creator = pubkey(), feeRecipient = pubkey();
const fees = {lpFeeBps: new BN(20), protocolFeeBps: new BN(80), creatorFeeBps: new BN(30)};
const feeConfig = {feeTiers: [{marketCapLamportsThreshold: new BN(0), fees}], flatFees: fees,
  stableFeeTiers: [], exoticFlatFees: fees};

function fixture(t, overrides={}) {
  const global = {feeRecipient, feeRecipients: Array.from({length: 7}, pubkey), feeBasisPoints: new BN(100),
    creatorFeeBasisPoints: new BN(30), creatorFeeConfigurable: false};
  const curve = {virtualQuoteReserves: new BN('32000000000'), virtualTokenReserves: new BN('1000000000000000'),
    realTokenReserves: new BN('700000000000000'), realQuoteReserves: new BN('2000000000'),
    tokenTotalSupply: new BN('1000000000000000'), complete: false, creator, isMayhemMode: false,
    quoteMint: PublicKey.default, creatorFeeBps: new BN(0), ...overrides};
  const data = Buffer.alloc(82);
  MintLayout.encode({mintAuthorityOption: 0, mintAuthority: PublicKey.default, supply: 1000000000000000n,
    decimals: 6, isInitialized: true, freezeAuthorityOption: 0, freezeAuthority: PublicKey.default}, data);
  const connection = {
    getAccountInfo: async () => ({owner: TOKEN_PROGRAM_ID, data, lamports: 2000000, executable: false}),
    getLatestBlockhash: async () => ({blockhash: pubkey().toBase58(), lastValidBlockHeight: 1234}),
    simulateTransaction: async () => ({context: {slot: 42}, value: {err: null, accounts: [{lamports: 901000000}]}}),
    getBalance: async () => 1000000000,
  };
  t.mock.method(OnlinePumpSdk.prototype, 'fetchGlobal', async () => global);
  t.mock.method(OnlinePumpSdk.prototype, 'fetchFeeConfig', async () => feeConfig);
  t.mock.method(OnlinePumpSdk.prototype, 'fetchBuyState', async () => ({bondingCurve: curve,
    bondingCurveAccountInfo: {data: Buffer.alloc(256)}, associatedUserAccountInfo: null,
    quoteMint: NATIVE_MINT, quoteTokenProgram: TOKEN_PROGRAM_ID}));
  const request = {action: 'BUILD', side: 'BUY', mint: mint.toBase58(), public_key: user.toBase58(),
    creator: creator.toBase58(), budget_lamports: '100000000', launch_price: 0.000000032};
  return {connection, request, curve};
}

test('integer and output/chase boundaries', () => {
  assert.equal(integer('123', 'value'), 123n);
  for (const value of ['0', '-1', 123, '1e9', '18446744073709551616'])
    assert.throws(() => integer(value, 'value'));
  assert.equal(guards({expected: 101n, current: 66n, price: 1.5, launchPrice: 1}), 66n);
  assert.throws(() => guards({expected: 101n, current: 65n, price: 1, launchPrice: 1}));
  assert.throws(() => guards({expected: 100n, current: 100n, price: 1.50001, launchPrice: 1}));
});

test('actual SDK builds unsigned exact-input buy with protected minimum and budget', async t => {
  const {connection, request} = fixture(t);
  const result = await handle(request, connection);
  assert.equal(result.simulation_passed, true);
  const tx = VersionedTransaction.deserialize(Buffer.from(result.transaction_base64, 'base64'));
  assert.ok(tx.signatures.every(s => s.every(n => n === 0)));
  assert.equal(tx.message.staticAccountKeys[0].toBase58(), user.toBase58());
  const swap = tx.message.compiledInstructions.find(i => tx.message.staticAccountKeys[i.programIdIndex].equals(PUMP_PROGRAM_ID));
  const exact = pumpIdl.instructions.find(i => i.name === 'buy_exact_quote_in_v2');
  assert.deepEqual([...swap.data.slice(0, 8)], exact.discriminator);
  assert.equal(Buffer.from(swap.data).readBigUInt64LE(8), 93995000n);
  assert.equal(Buffer.from(swap.data).readBigUInt64LE(16), BigInt(result.minimum_tokens_raw));
  assert.ok(Buffer.from(result.transaction_base64, 'base64').length <= 1232);
});

test('simulation failure and excess debit stop construction', async t => {
  const {connection, request} = fixture(t);
  connection.simulateTransaction = async () => ({value: {err: 'bad instruction'}});
  await assert.rejects(handle(request, connection), /SIMULATION_FAILED/);
  connection.simulateTransaction = async () => ({context: {slot: 1}, value: {err: null, accounts: [{lamports: 1}]}});
  await assert.rejects(handle(request, connection), /SIMULATION_BUDGET_EXCEEDED/);
});

test('actual SDK curve sell uses exact tokens and nonzero min SOL', async t => {
  const {connection, request} = fixture(t);
  const result = await handle({...request, side: 'SELL', tokens_raw: '100000000000'}, connection);
  const tx = VersionedTransaction.deserialize(Buffer.from(result.transaction_base64, 'base64'));
  const swap = tx.message.compiledInstructions.find(i => tx.message.staticAccountKeys[i.programIdIndex].equals(PUMP_PROGRAM_ID));
  const data = Buffer.from(swap.data);
  assert.equal(data.readBigUInt64LE(8), 100000000000n);
  assert.ok(data.readBigUInt64LE(16) > 0n);
});

test('migrated curves cannot be bought', async t => {
  const {connection, request} = fixture(t, {complete: true});
  await assert.rejects(handle(request, connection), /CURVE_MIGRATED/);
});

test('actual SDK builds canonical migrated AMM sell and closes temporary WSOL', async t => {
  const {connection, request} = fixture(t, {complete: true});
  const state = {
    globalConfig: {protocolFeeRecipients: Array.from({length: 8}, pubkey), protocolFeeBasisPoints: new BN(80),
      buybackFeeRecipients: Array.from({length: 8}, pubkey),
      lpFeeBasisPoints: new BN(20), coinCreatorFeeBasisPoints: new BN(30), creatorFeeConfigurable: false},
    feeConfig, poolKey: canonicalPumpPoolPda(mint), poolAccountInfo: {data: Buffer.alloc(1024)},
    pool: {baseMint: mint, quoteMint: NATIVE_MINT, creator: pumpPoolAuthorityPda(mint), coinCreator: creator,
      poolBaseTokenAccount: pubkey(), poolQuoteTokenAccount: pubkey(), isMayhemMode: false,
      isCashbackCoin: false, virtualQuoteReserves: new BN(0), creatorFeeBps: new BN(0)},
    poolBaseAmount: new BN('100000000000000'), poolQuoteAmount: new BN('100000000000'),
    baseTokenProgram: TOKEN_PROGRAM_ID, quoteTokenProgram: TOKEN_PROGRAM_ID,
    baseMint: mint, baseMintAccount: {supply: 1000000000000000n}, user,
    userBaseTokenAccount: pubkey(), userQuoteTokenAccount: pubkey(),
    userBaseAccountInfo: {data: Buffer.alloc(165), owner: TOKEN_PROGRAM_ID}, userQuoteAccountInfo: null,
  };
  t.mock.method(OnlinePumpAmmSdk.prototype, 'swapSolanaState', async key => {
    assert.ok(key.equals(canonicalPumpPoolPda(mint)));
    return state;
  });
  const result = await handle({...request, side: 'SELL', tokens_raw: '100000000000'}, connection);
  const tx = VersionedTransaction.deserialize(Buffer.from(result.transaction_base64, 'base64'));
  assert.ok(tx.message.compiledInstructions.some(i => tx.message.staticAccountKeys[i.programIdIndex].equals(PUMP_AMM_PROGRAM_ID)));
  assert.ok(tx.message.compiledInstructions.some(i => tx.message.staticAccountKeys[i.programIdIndex].equals(TOKEN_PROGRAM_ID) && i.data[0] === 9));
});

test('unexpected instruction ABI is rejected', () => {
  assert.throws(() => exactQuoteInstruction({programId: PublicKey.default, data: Buffer.alloc(24), keys: []}, 1n, 1n));
});
