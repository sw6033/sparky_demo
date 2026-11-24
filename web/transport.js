"use strict";

async function readCascade(response, onRecord=()=>{}) {
  if(!response.ok){
    let message="语音连接暂时不稳定，请重试。";
    try{message=(await response.json()).error||message;}catch{}
    throw Object.assign(new Error(message),{code:response.status});
  }
  const reader=response.body.getReader(),decoder=new TextDecoder();
  let buffer="",state,audio,stopped,finished=false;
  function record(line){
    if(!line.trim())return;
    let event;
    try{event=JSON.parse(line);}catch{throw new Error("语音连接中断，请重试。");}
    if(event.type==="error")throw Object.assign(new Error(event.error),{code:event.code});
    if(event.type==="state")state=event.state;
    if(event.type==="stop")stopped=event.message;
    if(event.type==="audio"){
      if(!state || event.reply!==state.reply.id)throw new Error("语音回复已过期。");
      const bytes=Uint8Array.from(atob(event.audio),c=>c.charCodeAt(0));
      audio=new Blob([bytes],{type:"audio/wav"});
    }
    onRecord(event);
  }
  try{
    while(true){
      const {value,done}=await reader.read();
      buffer+=decoder.decode(value||new Uint8Array(),{stream:!done});
      if(buffer.length>12*1024*1024)throw new Error("语音响应过长。");
      let end;
      while((end=buffer.indexOf("\n"))>=0){const line=buffer.slice(0,end);buffer=buffer.slice(end+1);record(line);}
      if(done){finished=true;break;}
    }
    if(buffer.trim())record(buffer);
    if(stopped)return {stopped};
    if(!state || !audio)throw new Error("语音连接中断，请重试。");
    return {state,audio};
  }finally{if(!finished)await reader.cancel().catch(()=>{});reader.releaseLock();}
}
