"""Narrow offline emergency backstop; contextual care remains a model decision."""
import re
from . import language

KINDS={"danger","distress","disclosure"}

def immediate(text):
    text=text.casefold().strip()
    return bool(re.search(r"^(?:sparky[,! ]+)?i (?:just )?(?:swallowed|drank|ate) (?:a |some |the |a button )?(?:battery|batteries|bleach|cleaner|pills)\b",text)
        or re.search(r"^i (?:want to|am going to) (?:hurt|kill) myself\b",text)
        or re.search(r"^(?:我|我刚刚|我刚才)(?:吞了|吃了|喝了)(?:一颗|一些|一口)?(?:电池|清洁剂|漂白水|很多药)",text)
        or re.search(r"^我(?:想|要)(?:伤害自己|自杀)",text))

def fallback(state,kind):
    words={
      "danger":("Get a trusted grown-up beside you now and ask them to call emergency help. I'm glad you told me; you don't have to handle this alone.","现在请找一个你信任的大人到你身边，让大人马上联系急救。谢谢你告诉我，你不用一个人面对。"),
      "distress":("That sounds really hard. We can stop playing. Is there a grown-up you trust who can sit with you?","听起来你很难受。我们先不玩了。有没有你信任的大人可以陪在你身边？"),
      "disclosure":("I'm glad you told me. This isn't your fault. Please find a safe grown-up, like a teacher or another trusted adult, who can help keep you safe.","谢谢你告诉我，这不是你的错。请找一个让你觉得安全的大人，比如老师或其他你信任的人，帮你保护自己。")}
    return language.wording(state,*words[kind])

def learner(state):
    return state.get("learner",{"native_language":"en","native_level":"unknown","target_language":"zh"})

def validate_learner(value):
    if not isinstance(value,dict) or set(value)!={"native_language","native_level","target_language"}:
        raise ValueError("语言档案字段无效")
    if value["native_language"]!="en" or value["native_level"] not in {"early","confident","unknown"} or value["target_language"]!="zh":
        raise ValueError("语言档案取值无效")
    return dict(value)
