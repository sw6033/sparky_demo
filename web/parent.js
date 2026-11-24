"use strict";
const $ = id => document.getElementById(id);
let profile=localStorage.getItem("sparky.profile"), home, pin="", busy=false;
function status(s){$("parentStatus").textContent=s;}
function switchProfile(id){profile=id;localStorage.setItem("sparky.profile",id);}
async function ensureProfile(){if(!profile){const r=await api("/api/profile",{name:"小朋友"});switchProfile(r.id);}}
async function api(path, body) {
  const opts = body ? {method:"POST", headers:{"Content-Type":"application/json","X-Parent-Pin":pin}, body:JSON.stringify(body)} : {};
  const res = await fetch(path, opts);
  const value = await res.json();
  if (!res.ok) throw Object.assign(new Error(value.error || "请求失败。"), {code:res.status});
  return value;
}
function node(tag, text, css) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (css) element.className = css;
  return element;
}
function button(text, run, css) {
  const b = node("button",text,css);
  b.type="button"; b.onclick=() => Promise.resolve(run()).catch(e=>status(e.message)); b.disabled=busy;
  return b;
}
const kinds={other_choice:"选了其他选项",identified:"按指令选对目标",identified_with_help:"得到提示后选对目标",independent:"无提示说出目标句",imitation:"完整示范后跟读",supported:"得到帮助后表达",english_choice:"英语选择",meaning_with_frame:"用已配置句式选择",chinese_word:"说出中文词汇",interpreted:"模型理解的选择",tap:"点选参与",uncertain:"输入不确定",no_response:"暂未回应",declined:"拒绝练习"};
async function parentRefresh(){
  home=await api("/api/home");
  if(!home.profiles.some(p=>p.id===profile)) profile=home.profiles[0]?.id || null;
  const profileRow=$("profiles"); profileRow.replaceChildren();
  for(const p of home.profiles){
    const b=button((p.id===profile?"当前 · ":"")+p.name,async()=>{await switchProfile(p.id);await parentRefresh();});
    profileRow.append(b);
  }
  $("engine").textContent=(home.model?"已配置对话模型":"本机规则模式")+"。保存进度、已确认偏好和对话摘要。Cascade 语音发送至 OpenAI；不保存录音。";
  const data=profile?await api("/api/parent",{profile,op:"summary"}):{observations:[],memories:[]};
  $("nativeLevel").value=data.learner?.native_level||"unknown";
  $("summaryView").textContent=JSON.stringify(data.conversation_summary||{},null,2);
  $("observations").replaceChildren(...data.observations.map(r=>node("div",r.goal+" · "+(kinds[r.kind]||r.kind)+" × "+r.n)));
  if(!data.observations.length)$("observations").textContent="还没有学习观察。";
  $("memories").replaceChildren();
  for(const m of data.memories){
    const row=node("div",undefined,"line"); row.append(node("span",m.value),button("忘记",async()=>{await api("/api/parent",{profile,op:"forget",value:m.id});await syncParent();}));$("memories").append(row);
  }
  $("deleteProfile").disabled=!profile;
}
async function syncParent(){await parentRefresh();}
function parentAction(fn){return async e=>{e?.preventDefault();try{$("parentStatus").textContent="";await fn();}catch(error){$("parentStatus").textContent=error.message;}};}
$("newProfile").onsubmit=parentAction(async()=>{const field=$("newProfile").querySelector("input");const r=await api("/api/profile",{name:field.value.trim()});await switchProfile(r.id);field.value="";await parentRefresh();});
$("memoryForm").onsubmit=parentAction(async()=>{await ensureProfile();const field=$("memoryForm").querySelector("input");await api("/api/parent",{profile,op:"remember",value:field.value.trim()});field.value="";await syncParent();});
$("learnerForm").onsubmit=parentAction(async()=>{await ensureProfile();await api("/api/parent",{profile,op:"learner",value:{native_language:"en",target_language:"zh",native_level:$("nativeLevel").value}});status("语言档案已保存，下轮对话生效。");});
$("forgetContext").onclick=parentAction(async()=>{if(profile){await api("/api/parent",{profile,op:"forget_context"});await syncParent();}});
$("deleteProfile").onclick=parentAction(async()=>{if(!window.confirm("删除这个档案及其进度、偏好和学习记录？"))return;
  await api("/api/parent",{profile,op:"delete"});profile=null;localStorage.removeItem("sparky.profile");await parentRefresh();});
(async()=>{home=await api("/api/home");if(home.pin)pin=prompt("请输入家长 PIN")||"";await parentRefresh();})().catch(e=>status(e.message));
