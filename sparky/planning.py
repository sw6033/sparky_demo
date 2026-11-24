"""Preview legal tools before one core-agent call; commit remains in Store."""
from copy import deepcopy
from . import brain, diagnostics, language, runtime, segments

class Decision(tuple):
    def __new__(cls, action, data=None, reply=None):
        value=super().__new__(cls,(action,data))
        value.reply=reply
        return value

def signature(decision):
    action,data=decision
    if action=="answer":
        return {"intent":"choose","item":data[0]["id"]}
    if action in {"start","change_activity"}:
        return {"intent":action,**data}
    if action=="language":
        return {"intent":action,"preference":data}
    return {"intent":action}

def preview(state,event,catalog,decision):
    candidate=deepcopy(state)
    runtime.advance(candidate,event,catalog,decision)
    active=candidate["active"]
    required=[]
    forbidden=[]
    action=decision[0]
    previous=state["active"]
    if action=="help" and previous:
        item=runtime.expected(previous) or runtime.candidates(previous)[0]
        required=[item["forms"][0]]
    elif action=="meaning" and previous:
        required=[term for item in runtime.candidates(previous) for term in (item["zh"],item["en"])]
    elif active and action!="chat":
        target=runtime.expected(active)
        if target:
            required=[target["zh"]]
            forbidden=[target["en"]]
    if active and action not in {"help","meaning"}:
        forbidden.extend(form for item in runtime.candidates(active) for form in item["forms"])
    view=runtime.public(candidate)
    capability={}
    if action in {"start","change_activity","resume","return_story"} and active:
        capability={"capability":active["lesson"].get("capabilities",{}).get(active["mode"],active["lesson"]["title"])}
    # A generic fallback question is not a chat exemplar; it biases every reply
    # toward echoing the child and asking another question.
    draft={"draft_reply":candidate["reply"]["text"]} if action!="chat" else {}
    return {**signature(decision),**capability,**draft,
            "after":{"activity":view["active"],"paused":view["paused"],"recent":view["recent"]},
            "required_verbatim":required,"forbidden_verbatim":forbidden}

def tools_for(state,event,catalog,local):
    a=state["active"]
    paused=state["paused"]
    choices=[("chat",None),("finish",None)]
    choices.extend(("language",v) for v in ("bilingual","english","chinese"))
    if a:
        choices.extend((v,None) for v in ("help","meaning","pause","refuse","clarify"))
        for item in runtime.candidates(a):
            category="interpreted"
            if local[0]=="answer" and local[1][0]["id"]==item["id"]:
                category=local[1][1]
            choices.append(("answer",(item,category)))
        if a["mode"]=="A" and paused:
            choices.append(("return_story",None))
    elif paused:
        choices.append(("resume",None))
    if catalog:
        for lesson in catalog.lessons.values():
            for mode in ("A","B"):
                if not a and not paused:
                    action="start"
                elif (a or paused)["lesson"]["id"]==lesson["id"]:
                    if (a or paused)["mode"]==mode:
                        continue
                    if (a or paused)["mode"]=="B" and mode=="A" and not (a and paused):
                        action="start"
                    else:
                        continue
                else:
                    action="change_activity"
                choices.append((action,{"lesson":lesson["id"],"mode":mode}))
    result=[]
    for decision in choices:
        try:
            result.append((decision,preview(state,event,catalog,decision)))
        except ValueError:
            continue
    return result

def reply_error(reply,contract):
    if not isinstance(reply,str) or not reply.strip() or len(reply)>600:
        return "invalid_reply"
    if any(term not in reply for term in contract["required_verbatim"]):
        return "missing_required_teaching_text"
    if any(term.casefold() in reply.casefold() for term in contract["forbidden_verbatim"]):
        return "revealed_answer"
    return None

def plan(state,event,memory,history,learning,catalog):
    local=runtime.local_propose(state,event,memory,history,learning,catalog)
    action,data=local
    # Explicit language and low-quality input need no model to be safe/reliable.
    if action in {"language","silence"} or event.get("speaker","child")!="child" or event.get("confidence",1)<0.55 or event["type"]=="silence":
        diagnostics.note("routing",source="local",action=action)
        return Decision(*local)
    options=tools_for(state,event,catalog,local)
    forced=event["type"]!="utterance" or action in {"answer","start","change_activity","pause","finish","refuse","resume","return_story"}
    if event["type"]=="start":
        local=("start",{"lesson":event["lesson"],"mode":event["mode"]})
    if event["type"]=="select":
        # Clicks preserve their evidence category and don't ask a model to choose.
        return Decision(*local)
    if forced:
        options=[(local,preview(state,event,catalog,local))]
    a=state["active"]
    context={"language_preference":language.preference(state),"recent_turns":list(history),
        "conversation_summary":state.get("conversation_summary",{}),"session_notes":state.get("session_notes",[]),
        "delivery_context":state["reply"],"confirmed_preferences":list(memory),"learning_observations":list(learning),
        "last_activity":state["recent"],"activity":a["lesson"]["title"] if a else None,
        "question":runtime.prompt(a,state) if a else None,
        "options":[{k:i[k] for k in ("id","zh","en")} for i in runtime.candidates(a)] if a else [],
        "available_actions":[spec for _,spec in options],
        "available_lessons":[{"id":l["id"],"title":l["title"],"capabilities":l.get("capabilities",{})} for l in catalog.lessons.values()] if catalog else []}
    recent_questions=sum(any(mark in turn.get("reply","") for mark in ("?","？")) for turn in list(history)[-3:])
    context["reply_guidance"]={
        "recent_replies_with_questions":recent_questions,
        "chat":"When the child shares an experience, respond with one brief, relevant comment and leave space. "
            "Do not repeat their sentence or append a follow-up question by habit. "
            "Answer their question directly when they ask one. Activity choices and necessary clarification may still be questions.",
        "sharing_response":"Prefer a statement, especially after recent questions."}
    if forced:
        context["forced_action"]=signature(local)
    text=event.get("text",event["type"])
    proposal=brain.propose(text,context)
    if not proposal:
        diagnostics.note("routing",source="fallback",action=local[0])
        return Decision(*local)
    chosen=next(((d,c) for d,c in options if all(proposal.get(k)==v for k,v in signature(d).items())),None)
    if not chosen:
        diagnostics.note("validation",status="action_rejected",intent=proposal.get("intent"))
        return Decision(*local)
    decision,contract=chosen
    reply=proposal.get("reply")
    if reply is None:
        # Accept legacy action-only adapters; runtime still owns the fallback.
        diagnostics.note("routing",source="model_action",action=decision[0])
        return Decision(*decision)
    error=reply_error(reply,contract)
    if error:
        diagnostics.note("validation",status=error)
        corrected=brain.propose(text,{**context,"available_actions":[contract],"forced_action":signature(decision),"repair_error":error})
        if corrected and all(corrected.get(k)==v for k,v in signature(decision).items()) and not reply_error(corrected.get("reply"),contract):
            reply=corrected["reply"]
        else:
            reply=None
    diagnostics.note("routing",source="model",action=decision[0],surface="generated" if reply else "fallback")
    return Decision(*decision,reply=reply.strip() if reply else None)

def finalize(state,decision):
    if getattr(decision,"reply",None):
        state["reply"]["text"]=decision.reply
        state["reply"]["segments"]=segments.split(decision.reply)
    reply=segments.attach(state["reply"])
    for a in (state["active"],state["paused"]):
        if a and a["hint"] and a["hint"]["id"]==reply["id"]:
            item=next(i for i in a["lesson"]["items"] if i["id"]==a["hint"]["item"])
            phrase=item["forms"][0] if a["hint"]["kind"]=="help" else item["zh"]
            a["hint"]["segments"]=[s["index"] for s in reply["segments"] if phrase in s["text"]]
