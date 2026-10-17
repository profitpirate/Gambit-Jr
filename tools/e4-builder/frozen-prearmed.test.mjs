import test from 'node:test';
import assert from 'node:assert/strict';
import BN from 'bn.js';
import {Connection, Keypair, PublicKey, VersionedTransaction} from '@solana/web3.js';
import {MintLayout, TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID, NATIVE_MINT, getAssociatedTokenAddressSync} from '@solana/spl-token';
import {OnlinePumpSdk, PUMP_SDK, pumpIdl, PUMP_PROGRAM_ID, PUMP_FEE_PROGRAM_ID, feeSharingConfigPda, getPumpFeeProgram,
  GLOBAL_PDA, PUMP_FEE_CONFIG_PDA, bondingCurvePda} from '@pump-fun/pump-sdk';
import {OnlinePumpAmmSdk, PUMP_AMM_PROGRAM_ID, canonicalPumpPoolPda, pumpPoolAuthorityPda} from '@pump-fun/pump-swap-sdk';
import {handle, guards, integer, exactQuoteInstruction, verifyBuyCreator, readSnapshot, retryableBuilderError, routeRequest, rpcCooldownMs} from './frozen-prearmed.mjs';

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
    getLatestBlockhashAndContext: async () => ({context:{slot:42},value:{blockhash:pubkey().toBase58(),lastValidBlockHeight:1234}}),
    simulateTransaction: async () => ({context: {slot: 42}, value: {err: null, accounts: [{lamports: 901000000}]}}),
    getBalance: async () => 1000000000,
  };
  connection.getMultipleAccountsInfoAndContext = async (addresses, commitment) => {
    assert.equal(commitment, 'confirmed');
    assert.deepEqual(addresses.slice(0,5).map(k=>k.toBase58()),
      [mint,GLOBAL_PDA,PUMP_FEE_CONFIG_PDA,bondingCurvePda(mint),feeSharingConfigPda(mint)].map(k=>k.toBase58()));
    return {context:{slot:42},value:[await connection.getAccountInfo(mint),
      {owner:PUMP_PROGRAM_ID,data:Buffer.alloc(100),executable:false},
      {owner:PUMP_FEE_PROGRAM_ID,data:Buffer.alloc(100),executable:false},
      {owner:PUMP_PROGRAM_ID,data:Buffer.alloc(256),executable:false},
      curve.creator.equals(feeSharingConfigPda(mint)) ? await connection.getAccountInfo(feeSharingConfigPda(mint)) : null,
      null,null]};
  };
  t.mock.method(PUMP_SDK, 'decodeGlobal', () => global);
  t.mock.method(PUMP_SDK, 'decodeFeeConfig', () => feeConfig);
  t.mock.method(PUMP_SDK, 'decodeBondingCurve', () => curve);
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

test('quote account failure exposes a stage code without provider credentials', async t => {
  const {connection, request} = fixture(t);
  connection.getAccountInfo = async () => { throw new Error('https://rpc.test/?key=SECRET'); };
  await assert.rejects(handle({...request, action: 'QUOTE'}, connection), error =>
    error.message === 'QUOTE_STATE_UNAVAILABLE' && !error.message.includes('SECRET'));
});

test('SDK state failure preserves a safe, specific quote reason', async t => {
  const {connection, request} = fixture(t);
  connection.getMultipleAccountsInfoAndContext = async () => { throw new Error('private provider detail'); };
  await assert.rejects(handle({...request, action: 'QUOTE'}, connection), /QUOTE_STATE_UNAVAILABLE/);
});

async function sharingAccount(overrides={}) {
  // Encode the real SDK account layout; do not mock the decoder or identity check.
  const program = getPumpFeeProgram(new Connection('http://127.0.0.1:8899'));
  const data = await program.coder.accounts.encode('sharingConfig', {
    bump: 255, version: 2, status: {active:{}}, mint, admin: creator, adminRevoked: true,
    shareholders: [{address: pubkey(), shareBps: 10000}], ...overrides,
  });
  return {owner: PUMP_FEE_PROGRAM_ID, data, executable: false, lamports: 1000000};
}

test('direct creator proof does not require another RPC read', async () => {
  const connection = {getAccountInfo: () => assert.fail('Unnecessary proof fetch')};
  assert.equal((await verifyBuyCreator(connection, mint, creator, creator.toBase58())).creator_verification, 'DIRECT_WALLET');
});

for (const revoked of [false, true]) test(`fee-sharing identity resolves the recorded admin, revoked=${revoked}`, async () => {
  const account = await sharingAccount({adminRevoked: revoked});
  const connection = {getAccountInfo: async (address, commitment) => {
    assert.ok(address.equals(feeSharingConfigPda(mint)));
    assert.equal(commitment, 'confirmed');
    return account;
  }};
  const proof = await verifyBuyCreator(connection, mint, feeSharingConfigPda(mint), creator.toBase58());
  assert.equal(proof.creator_verification, 'FEE_SHARING_AUTHORITY');
  assert.equal(proof.fee_sharing_admin, creator.toBase58());
  assert.equal(proof.fee_sharing_admin_revoked, revoked);
});

test('a different wallet or another mint sharing PDA is still rejected', async () => {
  for (const changed of [pubkey(), feeSharingConfigPda(pubkey())])
    await assert.rejects(verifyBuyCreator({}, mint, changed, creator.toBase58()), /QUOTE_CREATOR_CHANGED/);
});

test('missing creator never disables identity validation', async () => {
  await assert.rejects(verifyBuyCreator({}, mint, creator, undefined), /QUOTE_CREATOR_REQUIRED/);
});

test('missing or unavailable sharing state fails closed with a safe reason', async () => {
  for (const getAccountInfo of [async()=>null, async()=>{throw new Error('https://rpc/?token=SECRET');}])
    await assert.rejects(verifyBuyCreator({getAccountInfo}, mint, feeSharingConfigPda(mint), creator.toBase58()),
      error => error.message==='QUOTE_CREATOR_PROOF_UNAVAILABLE' && !error.message.includes('SECRET'));
});

test('wrong owner, mint, executable account and corrupt data cannot establish creator identity', async () => {
  const valid = await sharingAccount();
  const invalid = [{...valid, owner: pubkey()}, {...valid, executable: true},
    {...valid, data: Buffer.alloc(32)}, await sharingAccount({mint: pubkey()})];
  for (const account of invalid)
    await assert.rejects(verifyBuyCreator({getAccountInfo:async()=>account}, mint, feeSharingConfigPda(mint), creator.toBase58()),
      /QUOTE_CREATOR_PROOF_INVALID/);
});

test('a different sharing admin remains rejected', async () => {
  const account = await sharingAccount({admin: pubkey()});
  await assert.rejects(verifyBuyCreator({getAccountInfo:async()=>account}, mint, feeSharingConfigPda(mint), creator.toBase58()),
    /QUOTE_CREATOR_CHANGED/);
});

test('actual SDK quote and unsigned buy support verified fee sharing', async t => {
  const {connection,request} = fixture(t,{creator:feeSharingConfigPda(mint)});
  const getMint = connection.getAccountInfo;
  const account = await sharingAccount();
  connection.getAccountInfo = async address => address.equals(feeSharingConfigPda(mint)) ? account : getMint();
  const quote = await handle({...request,action:'QUOTE'},connection);
  assert.equal(quote.creator_verification,'FEE_SHARING_AUTHORITY');
  const built = await handle({...request,expected_tokens_raw:quote.quoted_tokens_raw},connection);
  assert.equal(built.simulation_passed,true);
  assert.equal(built.fee_sharing_admin,creator.toBase58());
  const tx = VersionedTransaction.deserialize(Buffer.from(built.transaction_base64,'base64'));
  assert.ok(tx.signatures.every(s=>s.every(n=>n===0)));
});

test('creator authority is rechecked between quote and build', async t => {
  const {connection,request} = fixture(t,{creator:feeSharingConfigPda(mint)});
  const getMint = connection.getAccountInfo;
  let account = await sharingAccount();
  connection.getAccountInfo = async address => address.equals(feeSharingConfigPda(mint)) ? account : getMint();
  await handle({...request,action:'QUOTE'},connection);
  account = await sharingAccount({admin:pubkey()});
  connection.simulateTransaction = async()=>assert.fail('Changed authority reached simulation');
  await assert.rejects(handle(request,connection),/QUOTE_CREATOR_CHANGED/);
});

test('sell construction stays available when creator authority has changed', async t => {
  const {connection,request} = fixture(t,{creator:feeSharingConfigPda(mint)});
  const built = await handle({...request,side:'SELL',tokens_raw:'100000000000'},connection);
  assert.equal(built.simulation_passed,true);
});

test('warmup loads the lane without using a provider', async () => {
  assert.equal((await handle({action:'WARMUP'},{})).ok,true);
});

test('mutable state is fetched again for every quote and build', async t => {
  const {connection,request}=fixture(t);let reads=0;
  const original=connection.getMultipleAccountsInfoAndContext;
  connection.getMultipleAccountsInfoAndContext=async(...a)=>{reads++;return original(...a);};
  const q=await handle({...request,action:'QUOTE'},connection);
  assert.equal(reads,1);assert.equal(q.state_slot,42);
  assert.ok(q.builder_timings.state_read_ms>=0);
  await handle({...request,expected_tokens_raw:q.quoted_tokens_raw},connection);
  assert.equal(reads,2);
});

test('snapshot rejects missing or wrong-owner state before instructions', async t => {
  const {connection,request}=fixture(t);const original=connection.getMultipleAccountsInfoAndContext;
  for (const index of [1,2,3]) {
    for (const mode of ['missing','owner','executable']) {
      connection.getMultipleAccountsInfoAndContext=async(...a)=>{
        const r=await original(...a);
        r.value[index]=mode==='missing'?null:{...r.value[index],...(mode==='owner'?{owner:pubkey()}:{executable:true})};return r;
      };
      await assert.rejects(handle(request,connection),/QUOTE_(STATE|FEES)_UNAVAILABLE/);
    }
  }
});

test('snapshot validates context and both supported token account choices', async t => {
  const {connection}=fixture(t);const original=connection.getMultipleAccountsInfoAndContext;
  for (const [program,index] of [[TOKEN_PROGRAM_ID,5],[TOKEN_2022_PROGRAM_ID,6]]) {
    const ata={owner:program,data:Buffer.alloc(165),executable:false};
    connection.getMultipleAccountsInfoAndContext=async(...a)=>{
      assert.ok(a[0][index].equals(getAssociatedTokenAddressSync(mint,user,true,program)));
      const r=await original(...a);r.value[0].owner=program;r.value[index]=ata;return r;
    };
    const snapshot=await readSnapshot(connection,mint,user);
    assert.ok(snapshot.tokenProgram.equals(program));
    assert.equal(snapshot.state.associatedUserAccountInfo,ata);
  }
  connection.getMultipleAccountsInfoAndContext=async(...a)=>{const r=await original(...a);r.context.slot=0;return r;};
  await assert.rejects(readSnapshot(connection,mint,user),/QUOTE_STATE_UNAVAILABLE/);
  connection.getMultipleAccountsInfoAndContext=async(...a)=>{const r=await original(...a);r.value.pop();return r;};
  await assert.rejects(readSnapshot(connection,mint,user),/QUOTE_STATE_UNAVAILABLE/);
});

test('definitive rejections stop fallback while unavailable state can recover',()=>{
  for(const reason of ['ENTRY_CHASE_GUARD_REJECTED','ENTRY_OUTPUT_GUARD_REJECTED','CURVE_MIGRATED','QUOTE_CREATOR_CHANGED'])
    assert.equal(retryableBuilderError(new Error(reason)),false);
  for(const reason of ['QUOTE_STATE_UNAVAILABLE','QUOTE_MINT_UNAVAILABLE','QUOTE_CREATOR_PROOF_UNAVAILABLE','timeout'])
    assert.equal(retryableBuilderError(new Error(reason)),true);
});

test('actual quote rejection retains current price, reference and bank slot',async t=>{
  const {connection,request}=fixture(t);
  await assert.rejects(handle({...request,action:'QUOTE',launch_price:1e-30},connection),error=>{
    assert.equal(error.message,'ENTRY_CHASE_GUARD_REJECTED');
    assert.equal(error.quote_facts.launch_price,1e-30);
    assert.equal(error.quote_facts.state_slot,42);
    assert.ok(error.quote_facts.curve_price>0 && error.quote_facts.price_multiple>1.5);
    return true;
  });
});

test('rate-limited provider is skipped until cooldown expires',async t=>{
  const {connection,request}=fixture(t);let calls=0;
  const limited={getMultipleAccountsInfoAndContext:async()=>{calls++;throw new Error('rate limited');}};
  const cooldowns=[200,0];
  const result=await routeRequest({...request,action:'QUOTE'},[limited,connection],cooldowns,()=>100);
  assert.equal(calls,0);assert.equal(result.provider_index,1);
  await routeRequest({...request,action:'QUOTE'},[limited,connection],cooldowns,()=>201);
  assert.equal(calls,1);
});

test('price rejection never consults a potentially older fallback state',async t=>{
  const {connection,request}=fixture(t);
  const forbidden={getMultipleAccountsInfoAndContext:async()=>assert.fail('Policy bypass')};
  await assert.rejects(routeRequest({...request,action:'QUOTE',launch_price:1e-30},[connection,forbidden],[],()=>1),/ENTRY_CHASE/);
});

test('all capped providers produce a safe error without any RPC call',async()=>{
  await assert.rejects(routeRequest({action:'QUOTE'},[{},{}],[100,100],()=>1),/QUOTE_RPC_COOLDOWN/);
  assert.equal(rpcCooldownMs('60'),60000);assert.equal(rpcCooldownMs('SECRET'),60000);
  assert.equal(rpcCooldownMs('9999999'),3600000);
});

test('BUILD starts blockhash and account reads concurrently, QUOTE does not read blockhash',async t=>{
  const {connection,request}=fixture(t);
  const original=connection.getMultipleAccountsInfoAndContext;
  let release,started=false,calls=0;
  connection.getLatestBlockhashAndContext=async()=>{
    started=true;calls++;
    await new Promise(resolve=>{release=resolve;});
    return {context:{slot:42},value:{blockhash:pubkey().toBase58(),lastValidBlockHeight:1234}};
  };
  connection.getMultipleAccountsInfoAndContext=async(...args)=>{
    assert.equal(started,true);release();return original(...args);
  };
  await handle(request,connection);assert.equal(calls,1);
  connection.getMultipleAccountsInfoAndContext=original;
  await handle({...request,action:'QUOTE'},connection);assert.equal(calls,1);
});

test('older prefetched bank is reread with minimum snapshot context',async t=>{
  const {connection,request}=fixture(t);const options=[];
  connection.getLatestBlockhashAndContext=async config=>{
    options.push(config);
    return {context:{slot:options.length===1?41:42},value:{blockhash:pubkey().toBase58(),lastValidBlockHeight:1234}};
  };
  await handle(request,connection);
  assert.deepEqual(options,[{commitment:'confirmed'},{commitment:'confirmed',minContextSlot:42}]);
});

test('stale bank even after refresh cannot reach simulation',async t=>{
  const {connection,request}=fixture(t);
  connection.getLatestBlockhashAndContext=async()=>({context:{slot:41},value:{blockhash:pubkey().toBase58(),lastValidBlockHeight:1234}});
  connection.simulateTransaction=async()=>assert.fail('Stale blockhash reached simulation');
  await assert.rejects(handle(request,connection),/BLOCKHASH_CONTEXT_STALE/);
});

test('failed speculative read cannot mask a definitive price rejection',async t=>{
  const {connection,request}=fixture(t);
  connection.getLatestBlockhashAndContext=async()=>{throw new Error('transport unavailable');};
  await assert.rejects(handle({...request,launch_price:1e-30},connection),/ENTRY_CHASE/);
});
