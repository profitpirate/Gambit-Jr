import test from 'node:test';
import assert from 'node:assert/strict';
import {RpcReadBudget,createReadFetch} from './rpc-read-budget.mjs';

test('adaptive limits need evidence, remain bounded and retain only 64 samples',()=>{
  const b=new RpcReadBudget();assert.equal(b.deadline('getMultipleAccounts'),1000);
  for(let i=0;i<100;i++)b.observe('getMultipleAccounts',25);
  assert.equal(b.samples.get('getMultipleAccounts').length,64);
  assert.equal(b.deadline('getMultipleAccounts'),400);
  for(let i=0;i<64;i++)b.observe('getMultipleAccounts',5000);
  assert.equal(b.deadline('getMultipleAccounts'),1500);
  for(let i=0;i<8;i++)b.observe('simulateTransaction',5);
  assert.equal(b.deadline('simulateTransaction'),1200);
  assert.equal(b.deadline('getBalance'),1000);
  b.observe('getBalance',NaN);assert.equal(b.samples.has('getBalance'),false);
});

test('complete response bodies are timed; JSON and headers survive unchanged',async()=>{
  const b=new RpcReadBudget();let clock=0;
  const body=JSON.stringify({jsonrpc:'2.0',id:1,result:{context:{slot:5},value:123}});
  const fn=createReadFetch({budget:b,clock:()=>clock,fetchImpl:async()=>({
    status:200,statusText:'OK',headers:new Headers({'content-type':'application/json'}),
    text:async()=>{clock=210;return body;}})});
  const response=await fn('https://example.test',{body:JSON.stringify({method:'getBalance'})});
  assert.equal(await response.text(),body);assert.deepEqual(b.samples.get('getBalance'),[210]);
});

test('rate-limit response triggers cooldown and does not train success latency',async()=>{
  const b=new RpcReadBudget();let retry;
  const fn=createReadFetch({budget:b,onRateLimit:value=>retry=value,
    fetchImpl:async()=>new Response('{"error":{"code":-32003}}',{status:429,headers:{'retry-after':'900'}})});
  const response=await fn('https://example.test',{body:'{"method":"getBalance"}'});
  assert.equal(response.status,429);assert.equal(retry,'900');assert.equal(b.samples.size,0);
});

test('transport forbids financial writes and cancellation reaches body consumption',async()=>{
  let calls=0;
  const forbidden=createReadFetch({fetchImpl:async()=>{calls++;}});
  await assert.rejects(forbidden('https://example.test',{body:'{"method":"sendTransaction"}'}),/FORBIDDEN/);
  assert.equal(calls,0);
  const abort=new AbortController();
  const fn=createReadFetch({timeoutSignal:()=>abort.signal,fetchImpl:async(_url,{signal})=>({
    status:200,headers:new Headers(),text:()=>new Promise((resolve,reject)=>{
      signal.addEventListener('abort',()=>reject(new Error('aborted')),{once:true});
      abort.abort();
    })})});
  await assert.rejects(fn('https://example.test',{body:'{"method":"getBalance"}'}),/aborted/);
});
