"""One companion, two activity modes. Activity decisions consume lesson data only."""
import uuid
from . import brain, language
from .content import compile_answers, norm

def uid():
    return uuid.uuid4().hex

def initial():
    return {"revision":0,"active":None,"paused":None,"recent":None,"language_preference":"bilingual",
            "reply":{"id":uid(),"text":"Hi, I'm Sparky! What would you like to do today?","delivery":"pending"}}

def say(state,text):
    state["reply"]={"id":uid(),"text":text,"delivery":"pending"}

def step(a):
    return a["lesson"]["rules"][a["mode"]]["steps"][a["index"]]

def candidates(a):
    items=a["lesson"]["items"]
    if step(a)["items"]=="selected":
        return [x for x in items if x["id"]==a["choice"]]
    return items

def expected(a):
    slot=step(a).get("expected")
    if slot:
        target=a["lesson"]["targets"][slot]
        return next(i for i in a["lesson"]["items"] if i["id"]==target)
    return None

def prompt(a,state=None):
    s=step(a)
    target=expected(a)
    if language.preference(state or {})!="chinese":
        # English scaffolding surrounds the Chinese exercise. Never reveal a
        # listening target's translation or the answer to a production task.
        fallback = ('Listen: "{target_zh}". Which one is it?' if target else
                    "How would you say it in Chinese?" if s["items"]=="selected" else
                    "Which one would you like to choose?")
        question=a["lesson"].get("prompts_en",{}).get(s["prompt"],fallback)
        if target:
            return question.format(target_zh=target["zh"])
        options=", ".join(item["en"] for item in candidates(a))
        return question + (" ("+options+")" if s["items"]=="selected" else " "+options+"?")
    question=a["lesson"]["prompts"][s["prompt"]]
    if target:
        question=question.format(target_zh=target["zh"],target_en=target["en"])
    if s["items"]=="selected":
        question += " ("+candidates(a)[0]["en"]+")"
    elif not target:
        options = "、".join(item["zh"] for item in candidates(a))
        question += f" 比如{options}。"
    return question

def match(a,text):
    value=norm(text)
    for item in candidates(a):
        answers=item.get("answers") or compile_answers(a["lesson"],item)
        if value in answers:
            return item,answers[value]
    return None,None

def lesson_request(value, catalog):
    if value in catalog.cue_words:
        return None
    prefixes = {"A": ("我想练习", "练习", "我想练", "练"), "B": ("我想玩", "我要玩", "我想学", "我想听", "玩", "学", "")}
    for mode, starters in prefixes.items():
        for lesson in catalog.lessons.values():
            names = [lesson["title"], *lesson.get("entry_names", [])]
            if mode=="B" and any(value == norm(prefix+phrase) for prefix in ("", "想", "我想", "我要", "我们来") for phrase in lesson.get("entry_phrases", [])):
                return {"lesson": lesson["id"], "mode": mode}
            if any(value == norm(prefix + name) for prefix in starters for name in names):
                return {"lesson": lesson["id"], "mode": mode}
    return None

def propose(state,event,memory=(),history=(),learning=(),catalog=None):
    """Explicit controls and exact curricular answers work without network access."""
    if event["type"]!="utterance":
        return event["type"],None
    if event.get("speaker","child")!="child" or event.get("confidence",1)<0.55:
        return "unclear",None
    text=event["text"].strip()
    if not text:
        return "silence",None
    v=norm(text)
    requested=language.explicit_request(text)
    if requested:
        return "language",requested
    if v.removesuffix("sparky") in {"你好","你你好","嗨","哈喽","hello","hi","hey","早上好","晚上好"}:
        return "chat",language.wording(state,"Hi! I'm here.","你好呀！我在呢。")
    controls={
        "finish":["结束","不玩了","stop","all done","别玩了"],
        "pause":["休息一下","暂停","等一下","pause"],
        "refuse":["我不想说","不想跟读","不想说","不练了","不想练","no thanks"],
        "help":["帮帮我","我不会","提示","help","help me"],
        "meaning":["什么意思","这是什么意思","what does that mean"],
    }
    for action,phrases in controls.items():
        if v in {norm(p) for p in phrases}:
            return action,None
    a=state["active"]
    if a and v in {"继续","继续故事","继续玩"}:
        if a["mode"]=="A" and state["paused"] and v=="继续故事":
            return "return_story", None
        return "chat",prompt(a,state)
    yes = v in {"好","好呀","好的","好啊","可以","想","要","yes","继续","继续玩"}
    if a and a["mode"]=="B" and v in {"练一句中文","练一句","我想练习","练中文","我想练中文"}:
        return "start", {"lesson":a["lesson"]["id"],"mode":"A"}
    if not a and state["paused"] and (yes or v in {"回来啦","继续故事","resume"}):
        return "resume", None
    if not a and not state["paused"] and catalog:
        if yes and state.get("invitation") in catalog.lessons:
            return "start", {"lesson":state["invitation"],"mode":"B"}
        activity = lesson_request(v, catalog)
        if activity:
            return "start", activity
    if catalog and (a or state["paused"]) and (not a or not match(a,text)[0]):
        request = lesson_request(v.removeprefix("换成").removeprefix("换到"), catalog)
        if request:
            current = a or state["paused"]
            if current["lesson"]["id"] == request["lesson"] and current["mode"] == request["mode"]:
                return ("chat", prompt(a,state)) if a else ("resume", None)
            if current["mode"]=="B" and request=={"lesson":current["lesson"]["id"],"mode":"A"}:
                return "start",request
            return "change_activity",request
    if a:
        item,category=match(a,text)
        if item:
            return "answer",(item,category)
        if "什么意思" in text or "what does" in text.lower():
            return "meaning",None
    cue = catalog.conversation_entry(text) if catalog and not state["paused"] else None
    context={"language_preference":language.preference(state),"recent_turns":list(history),"learning_observations":list(learning),"confirmed_preferences":list(memory),"last_activity":state["recent"],"activity":a["lesson"]["title"] if a else None,"question":prompt(a,state) if a else None,
             "options":[{"id":x["id"],"zh":x["zh"],"en":x["en"]} for x in candidates(a)] if a else []}
    if cue:
        # An example is optional context, not a mandatory Chinese reply.
        context["conversation_example"]={"word":cue["word"],"example":cue["offer"]}
    proposal=brain.propose(text,context)
    if proposal:
        intent=proposal["intent"]
        if intent=="language" and proposal.get("preference") in language.PREFERENCES:
            return "language",proposal["preference"]
        if intent=="choose" and a:
            item=next((x for x in candidates(a) if x["id"]==proposal.get("item")),None)
            if item:
                return "answer",(item,"interpreted")
        if intent in {"help","meaning","refuse","pause","finish","clarify"}:
            return intent,None
        if intent=="chat" and isinstance(proposal.get("reply"),str) and proposal["reply"].strip():
            return "chat",proposal["reply"].strip()[:200]
    if cue and language.preference(state)=="chinese":
        if any(p in text.lower().replace("’","'") for p in ("don't like","do not like","不喜欢")):
            return "chat","哦，你不喜欢。那你喜欢什么呀？"
        recent = [turn["reply"] for turn in history[-3:]]
        if cue["offer"] not in recent:
            return "chat",cue["offer"]
        return "chat",cue["follow"] if cue["follow"] not in recent else "嗯，我在听。"
    if not a and not state["paused"] and history and catalog:
        last_reply=history[-1]["reply"]
        if any(last_reply in (e["offer"],e["follow"]) for e in catalog.conversation):
            return "chat",language.wording(state,"I'm listening.","嗯，我在听。")
    if language.preference(state)!="chinese":
        # With no usable model result, do not inject a Chinese lesson just
        # because a keyword matched. Contextual bilingual teaching needs a model.
        return ("clarify" if a else "chat"),None
    if not a and catalog and not state["paused"]:
        first = next(iter(catalog.lessons.values()))
        return "invite", (first["id"], first.get("invitation", f"想听《{first['title']}》吗？"))
    return ("clarify" if a else "chat"),None

def observation(a,kind,item=None):
    goal=a["lesson"]["goals"][step(a)["goal"]]
    hint=a["hint"]
    return {"goal":goal["id"],"label":goal["label"],"lesson":a["lesson"]["id"],
            "version":a["lesson"]["digest"],"instance":a["id"],"mode":a["mode"],
            "kind":kind,"item":item["id"] if item else None,
            "help":hint["delivery"] if hint else "none"}

def start(state,event,catalog):
    mode=event["mode"]
    paused=state["paused"]
    if state["active"] and not (mode=="A" and state["active"]["mode"]=="B" and not paused):
        raise ValueError("请先结束或暂停当前活动。")
    parent=state["active"] or paused
    if parent and (parent["mode"]!="B" or mode!="A" or event["lesson"]!=parent["lesson"]["id"]):
        raise ValueError("请先恢复或结束保留的活动。")
    lesson=parent["lesson"] if parent else catalog.lessons.get(event["lesson"])
    if not lesson:
        raise ValueError("课程不存在。")
    target=(parent or {}).get("choice") or lesson["items"][0]["id"]
    if state["active"]:
        state["paused"]=parent
    a={"id":uid(),"mode":mode,"lesson":lesson,"index":0,"choice":target,"hint":parent.get("hint") if parent else None,"choices":[],"repair_count":0}
    state["active"]=a
    say(state,prompt(a,state))

def complete(state,a,reason):
    state["recent"]={"lesson":a["lesson"]["id"],"title":a["lesson"]["title"],
                     "mode":a["mode"],"choices":a["choices"],"result":reason}
    state["active"]=None
    if state["paused"]:
        return language.wording(state,"Would you like to return to our story?","想回到刚才的故事吗？")
    return language.wording(state,"We can take a break or choose another activity.","我们可以休息，也可以再选个活动。")

def advance(state,event,catalog,decision):
    action,data=decision
    a=state["active"]
    records=[]
    if action=="language":
        if data not in language.PREFERENCES:
            raise ValueError("未知语言偏好。")
        state["language_preference"]=data
        say(state,language.acknowledgement(data))
        state["revision"]+=1
        return records
    state.pop("invitation", None)
    if action=="start":
        start(state,data or event,catalog)
    elif action=="change_activity":
        complete(state,a or state["paused"],"changed")
        state["paused"]=None
        start(state,data,catalog)
    elif action in {"resume","return_story"}:
        if action=="return_story":
            complete(state,a,"returned_to_story")
            a=None
        if a or not state["paused"]:
            raise ValueError("当前没有可恢复的活动。")
        a=state["active"]=state["paused"]
        state["paused"]=None
        say(state,language.wording(state,"Welcome back! ","回来啦！")+prompt(a,state))
    elif action=="finish":
        target=a or state["paused"]
        if not target:
            say(state,language.wording(state,"Okay, let's take a break.","好的，我们休息一下。"))
        else:
            if not a:
                state["paused"]=None
            say(state,language.wording(state,"Okay, we'll stop here. ","好，今天先到这里。")+complete(state,target,"ended"))
    elif action=="invite":
        state["invitation"], invitation = data
        say(state,invitation)
    elif action=="chat":
        say(state,data or language.wording(state,"I'm here. What would you like to talk about?","我在听。你可以跟我打招呼，也可以选一个喜欢的活动。"))
    elif not a:
        say(state,language.wording(state,"We can choose an activity or take a break.","我们可以选个活动，也可以先休息。"))
    elif action=="pause":
        if state["paused"]:
            say(state,language.wording(state,"Okay, let's take a break. ","好的，先休息。")+complete(state,a,"paused_practice"))
        else:
            state["paused"],state["active"]=a,None
            say(state,language.wording(state,"I'll save our place. Tell me when you'd like to continue.","好，我记住玩到哪里了。想继续时就叫我。"))
    elif action=="refuse":
        records.append(observation(a,"declined"))
        if a["mode"]=="A":
            say(state,language.wording(state,"Okay. ","好的。")+complete(state,a,"declined"))
        else:
            say(state,language.wording(state,"That's okay. You can choose something or take a break.","好呀，先不练了。你可以说你想选什么，也可以休息。"))
    elif action in {"silence","unclear","clarify"}:
        records.append(observation(a,{"silence":"no_response","unclear":"uncertain","clarify":"uncertain"}[action]))
        policy=a["lesson"]["rules"][a["mode"]]["recovery"]
        a["repair_count"]=a.get("repair_count",0)+1
        key="wait" if action=="silence" else "unclear"
        if a["repair_count"]>policy["clarifications"]:
            key="release"
        english={"unclear":"I didn't catch that. You can say it again or ask for help.",
                 "wait":"Take your time. I'm listening.","release":"Let's take a break. Tell me when you're ready."}
        say(state,language.wording(state,english[key],policy[key]))
    elif action in {"help","meaning"}:
        options=candidates(a)
        item=expected(a) or options[0]
        if action=="meaning" and len(options)>1:
            msg="，".join(x["zh"]+" 是 "+x["en"] for x in options)+"。"
        elif action=="meaning":
            msg=item["zh"]+" 是 "+item["en"]+"。"
        else:
            msg="你可以说：“"+item["forms"][0]+"”。"
        if language.preference(state)!="chinese":
            if action=="meaning":
                msg="; ".join('"'+x["zh"]+'" means '+x["en"] for x in options)+"."
            else:
                msg='You can say: "'+item["forms"][0]+'".'
        say(state,msg)
        a["hint"]={"id":state["reply"]["id"],"delivery":"pending","kind":action,"item":item["id"]}
        if a["mode"]=="A" and state["paused"] and state["paused"]["choice"]==item["id"]:
            state["paused"]["hint"]=dict(a["hint"])
    elif action in {"select","answer"}:
        if action=="select":
            item=next((x for x in candidates(a) if x["id"]==event.get("item")),None)
            if not item:
                raise ValueError("这个选项不在当前步骤中。")
            category="tap"
        else:
            item,category=data
        target=expected(a)
        if target and target["id"]!=item["id"]:
            records.append(observation(a,"other_choice",item))
            say(state,language.wording(state,"Let's listen again. ","再听一听。")+prompt(a,state))
            state["revision"]+=1
            return records
        hint=a["hint"]
        kind={"english":"english_choice","word":"chinese_word","tap":"tap","interpreted":"interpreted","mixed":"meaning_with_frame"}.get(category)
        if category=="target":
            if not hint:
                kind="independent"
            elif hint["delivery"]=="complete" and hint["kind"]=="help" and hint["item"]==item["id"] and norm(event.get("text",""))==norm(item["forms"][0]):
                kind="imitation"
            else:
                kind="supported"
        if target:
            kind="identified_with_help" if hint else "identified"
        records.append(observation(a,kind,item))
        a["repair_count"]=0
        s=step(a)
        if s["accept"]=="target" and category!="target":
            say(state,language.wording(state,"Would you like to try saying it in Chinese? We can also stop here.","选好了！想试着说一句中文吗？也可以结束练习。"))
        else:
            if s["remember"]:
                a["choice"]=item["id"]
            a["choices"].append(item["id"])
            # Keep support when the same target is elicited again.
            if a["hint"] and a["hint"]["item"]!=item["id"]:
                a["hint"]=None
            a["index"]+=1
            rules=a["lesson"]["rules"][a["mode"]]["steps"]
            if a["index"]==len(rules):
                say(state,language.wording(state,"Okay! ","好呀！")+complete(state,a,"completed"))
            else:
                say(state,prompt(a,state))
    else:
        raise ValueError("未知动作。")
    state["revision"]+=1
    return records

def public(state):
    def view(a):
        if not a:
            return None
        lesson=a["lesson"]
        return {"lesson":lesson["id"],"title":lesson["title"],"icon":lesson["icon"],"mode":a["mode"],
                "step":a["index"]+1,"total":len(lesson["rules"][a["mode"]]["steps"]),
                "question":prompt(a,state),"goal":lesson["goals"][step(a)["goal"]]["label"],
                "options":[{k:x[k] for k in ("id","zh","icon")} for x in candidates(a)]}
    reply = dict(state["reply"])
    reply["text"] = conversation_copy(reply["text"])
    return {**state,"language_preference":language.preference(state),"reply":reply,"active":view(state["active"]),"paused":view(state["paused"])}

def conversation_copy(text):
    # Older checkpoints may contain prompts written for the original card UI.
    if text.startswith("我们一起玩") and "，好吗？你也可以告诉我想玩什么。" in text:
        return "我们去看看草丛里藏着谁，好吗？" if "动物" in text else "我们一起试试，好吗？"
    return (text.replace("你可以点一下，或者再说一次。","可以再说一次，也可以说「帮帮我」。")
            .replace("想选的时候点一下就好。","想好了再告诉我。")
            .replace("可以点一下来选","可以告诉我你的选择")
            .replace("也可以选下面的活动","也可以告诉我想玩什么"))


