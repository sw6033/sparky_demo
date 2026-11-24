"""One core conversation agent; validated proposals never directly mutate state."""
import json
import os
import time
import urllib.request
from pathlib import Path
from urllib.error import HTTPError
from .speech import tls_context
from . import diagnostics

PROMPT_DIR=Path(__file__).parent/"prompts"
CORE_PROMPT=(PROMPT_DIR/"core.txt").read_text().strip()
PROTOCOL_PROMPT=(PROMPT_DIR/"protocol.txt").read_text().strip()
SYSTEM_PROMPT=CORE_PROMPT+"\n\n"+PROTOCOL_PROMPT
SUMMARY_PROMPT=(Path(__file__).parent/"prompts"/"summary.txt").read_text()
TEACHING_STYLE=CORE_PROMPT

def configured():
    return bool(os.getenv("SPARKY_MODEL") and os.getenv("SPARKY_MODEL_URL"))

def request_json(system, data, purpose="decision"):
    started=time.monotonic()
    if not configured():
        diagnostics.note(purpose,status="unconfigured",ms=0)
        return None
    payload={"model":os.environ["SPARKY_MODEL"],"messages":[
        {"role":"system","content":system},
        {"role":"user","content":json.dumps(data,ensure_ascii=False)}],
        "temperature":0.2,"max_tokens":700}
    headers={"Content-Type":"application/json"}
    if os.getenv("SPARKY_MODEL_KEY"):
        headers["Authorization"]="Bearer "+os.environ["SPARKY_MODEL_KEY"]
    request=urllib.request.Request(os.environ["SPARKY_MODEL_URL"],data=json.dumps(payload).encode(),headers=headers)
    try:
        with urllib.request.urlopen(request,timeout=20,context=tls_context()) as response:
            response_data=json.loads(response.read(131072))
        result=json.loads(response_data["choices"][0]["message"]["content"])
        if not isinstance(result,dict):
            raise ValueError()
        diagnostics.note(purpose,status="ok",ms=diagnostics.elapsed(started))
        return result
    except HTTPError as exc:
        diagnostics.note(purpose,status="http_error",code=exc.code,ms=diagnostics.elapsed(started))
    except (TimeoutError,OSError):
        diagnostics.note(purpose,status="network_error",ms=diagnostics.elapsed(started))
    except (ValueError,KeyError,IndexError,TypeError):
        diagnostics.note(purpose,status="invalid_json",ms=diagnostics.elapsed(started))
    return None

def propose(text, context):
    prompt=SYSTEM_PROMPT
    if context.get("learner",{}).get("native_level")=="early" and context.get("language_preference")!="english":
        prompt+="\n\nCurrent child's teaching mode: IMMERSION. Their English explanation skills are still developing. For a request to learn a Chinese word or phrase, give one tiny concrete Mandarin example, perhaps a playful sound or action. Example approach: for a ball, ‘小球，滚呀滚。’ Do not define the word in English, spell pinyin, or ask the child to repeat. The example should itself answer the learning request. Ordinary sharing can remain conversation. A current explicit request for English and safety needs take priority. Apply this to the actual reply, not merely the chosen action."
    result=request_json(prompt,{"context":context,"utterance":text})
    if not result:
        return None
    intent=result.get("intent")
    if not isinstance(intent,str) or intent not in {"choose","help","meaning","refuse","pause","finish","clarify","language","chat","start","change_activity","resume","return_story","support"}:
        diagnostics.note("validation",status="invalid_intent")
        return None
    if intent=="support" and result.get("kind") not in {"danger","distress","disclosure"}:
        return None
    if intent=="language" and result.get("preference") not in ("english","chinese","bilingual"):
        diagnostics.note("validation",status="invalid_preference")
        return None
    return result

def summarize(previous, turns, notes):
    result=request_json(SUMMARY_PROMPT,{"previous_summary":previous,"older_turns":turns,"session_notes":notes},"summary")
    if not result or not isinstance(result.get("topic"),str):
        return None
    clean={"topic":result["topic"][:120]}
    for key in ("facts","open_threads"):
        values=result.get(key)
        if not isinstance(values,list) or any(not isinstance(v,str) for v in values):
            return None
        clean[key]=[v[:120] for v in values[:6]]
    return clean

