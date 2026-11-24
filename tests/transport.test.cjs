const test=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const read=vm.runInNewContext(fs.readFileSync(require.resolve('../web/transport.js'),'utf8')+'\nreadCascade', {TextDecoder,Uint8Array,Blob,atob});
const state={type:'state',state:{reply:{id:'r',text:'你好'}}},audio={type:'audio',reply:'r',audio:btoa('RIFF')};
const wire=rows=>rows.map(x=>JSON.stringify(x)+'\n').join('');
test('fragmented UTF8 and JSON records preserve early state and exact audio',async()=>{
  let controller;const events=[];
  const response=new Response(new ReadableStream({start(c){controller=c;}}));
  const pending=read(response,event=>events.push(event.type));
  for(const byte of new TextEncoder().encode(wire([state])))controller.enqueue(new Uint8Array([byte]));
  await new Promise(resolve=>setImmediate(resolve));assert.deepEqual(events,['state']);
  controller.enqueue(new TextEncoder().encode(wire([audio])));controller.close();
  const result=await pending;assert.equal(result.state.reply.text,'你好');assert.equal(await result.audio.text(),'RIFF');
});
test('truncation, stale audio and upstream errors never yield playable result',async()=>{
  await assert.rejects(read(new Response(wire([state]))),/连接中断/);
  await assert.rejects(read(new Response(wire([state,{...audio,reply:'old'}]))),/过期/);
  await assert.rejects(read(new Response(wire([state,{type:'error',error:'cancelled',code:409}]))),e=>e.code===409);
});
test('empty ASR produces stop rather than invented audio',async()=>{
  assert.equal((await read(new Response(wire([{type:'stop',message:'Try again'}])))).stopped,'Try again');
});
test('tunnel HTML failures and truncated JSON produce a useful error',async()=>{
  await assert.rejects(read(new Response('<!DOCTYPE html>',{status:502})),e=>e.code===502 && /连接暂时不稳定/.test(e.message));
  await assert.rejects(read(new Response('{"type":')),/连接中断/);
});
