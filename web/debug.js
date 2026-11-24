"use strict";
const $=id=>document.getElementById(id);
async function refresh(){
  if(!$("profiles").value)return;
  try{
    const response=await fetch("/api/debug",{method:"POST",headers:{"Content-Type":"application/json","X-Parent-Pin":$("pin").value},body:JSON.stringify({profile:$("profiles").value})});
    const data=await response.json();
    if(!response.ok)throw new Error(data.error);
    $("status").textContent=data.transcripts?"调试逐字稿记录已开启。":"仅记录结构化诊断；逐字稿记录未开启。";
    $("turns").replaceChildren();
    for(const turn of data.turns){
      const detail=document.createElement("details"),title=document.createElement("summary"),body=document.createElement("pre");
      title.textContent=`${new Date(turn.created*1000).toLocaleTimeString()} · ${turn.action||turn.summary_status||"语音"} · ${turn.turn} · 大脑 ${turn.brain_ms??"—"} ms · 首音 ${turn.client_metrics?.first_audio_ms??"—"} ms`;
      body.textContent=JSON.stringify(turn,null,2);detail.append(title,body);$("turns").append(detail);
    }
    $("summary").textContent=JSON.stringify({summary:data.summary,notes:data.notes},null,2);
    $("prompt").textContent=data.prompt;
  }catch(error){$("status").textContent=error.message;}
}
$("refresh").onclick=refresh;
$("profiles").onchange=refresh;
fetch("/api/home").then(r=>r.json()).then(home=>{
  for(const profile of home.profiles){const option=document.createElement("option");option.value=profile.id;option.textContent=profile.name;$("profiles").append(option);}
  if(!home.profiles.length)$("status").textContent="请先在小屋创建档案并进行一次对话。";
  else if(!home.pin)refresh();
}).catch(error=>{$("status").textContent=error.message;});
