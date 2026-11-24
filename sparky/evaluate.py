"""Opt-in real-model synthetic multi-turn evaluation, isolated from child data."""
import argparse
import json
import os
import re
import tempfile
import time
import uuid
from pathlib import Path
from . import brain
from .content import Catalog, ROOT
from .store import Store

def checks(state,expect):
    active=state["active"] or {}
    paused=state["paused"] or {}
    values={"lesson":active.get("lesson"),"mode":active.get("mode"),"paused":paused.get("lesson"),
            "idle":not active and not paused,"language":state["language_preference"],
            "care":state.get("care_kind"),"care_hold":bool(state.get("care_hold")),
            "chinese_present":bool(re.search(r"[\u3400-\u9fff]",state["reply"]["text"])),
            "english_only":not bool(re.search(r"[\u3400-\u9fff]",state["reply"]["text"]))}
    return {key:(expected in state["reply"]["text"] if key=="contains" else values[key]==expected) for key,expected in expect.items()}

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run",action="store_true",help="调用真实模型，会产生 API 用量")
    parser.add_argument("--cases",type=Path,default=ROOT/"evaluations"/"conversations.json")
    parser.add_argument("--output",type=Path,default=ROOT/".data"/"evaluation.json")
    parser.add_argument("--compare",type=Path)
    args=parser.parse_args()
    cases=json.loads(args.cases.read_text())
    if not args.run:
        print(json.dumps(cases,ensure_ascii=False,indent=2));return
    if not brain.configured() and os.getenv("OPENAI_API_KEY"):
        os.environ.setdefault("SPARKY_MODEL","gpt-4.1")
        os.environ.setdefault("SPARKY_MODEL_URL","https://api.openai.com/v1/chat/completions")
        os.environ.setdefault("SPARKY_MODEL_KEY",os.environ["OPENAI_API_KEY"])
    if not brain.configured():
        parser.error("未配置文本模型。请设置 SPARKY_MODEL / SPARKY_MODEL_URL / SPARKY_MODEL_KEY 或 OPENAI_API_KEY。")
    rows=[]
    with tempfile.TemporaryDirectory() as tmp:
        store=Store(Path(tmp)/"evaluation.db",Catalog())
        try:
            for case in cases:
                pid=store.create("evaluation",case["name"])
                if case.get("learner"):store.parent("evaluation",pid,"learner",case["learner"])
                for turn in case["turns"]:
                    started=time.monotonic()
                    event={"id":uuid.uuid4().hex,"revision":store.state("evaluation",pid)["revision"],"type":"utterance","text":turn["text"]}
                    state,_=store.event("evaluation",pid,event)
                    results=checks(state,turn["expect"])
                    trace=next(t for t in store.debug("evaluation",pid)["turns"] if t["turn"]==event["id"])
                    fallback=any(d.get("source")=="fallback" or d.get("status") in {"http_error","network_error","invalid_json"} for d in trace.get("decision",[]))
                    rows.append({"case":case["name"],"input":turn["text"],"reply":state["reply"]["text"],"checks":results,
                                 "passed":all(results.values()) and not fallback,"fallback":fallback,"ms":round((time.monotonic()-started)*1000,1),"trace":trace})
                    reply=state["reply"]
                    parts=[{}] if reply.get("speech_mode")=="whole" else reply["segments"]
                    for part in parts:
                        attempt=uuid.uuid4().hex
                        store.delivery("evaluation",pid,reply["id"],"started",attempt,part.get("index"))
                        store.delivery("evaluation",pid,reply["id"],turn.get("delivery","complete"),attempt,part.get("index"))
                        if turn.get("delivery")=="interrupted":break
        finally:
            store.close()
            store.summary_pool.shutdown(wait=True)
    report={"model":os.getenv("SPARKY_MODEL"),"passed":sum(r["passed"] for r in rows),"total":len(rows),
            "manual_review":"请人工检查语气、语言切换时机和回应相关性；自动检查不能替代对话质量评审。","turns":rows}
    if args.compare:
        previous=json.loads(args.compare.read_text())
        report["comparison"]={"previous_passed":previous["passed"],"previous_total":previous["total"],"current_passed":report["passed"]}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(f"{report['passed']}/{report['total']} turns passed; report: {args.output}")

if __name__=="__main__":
    main()
