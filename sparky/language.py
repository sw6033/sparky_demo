"""Persistent preference, not a rule engine for when to teach Chinese.

Only unambiguous language controls have an offline shortcut. The conversation
model handles paraphrases and decides when Chinese fits a bilingual conversation.
"""
import re

PREFERENCES = {"bilingual", "english", "chinese"}

def preference(state):
    return state.get("language_preference", "bilingual")

def wording(state, english, chinese):
    return chinese if preference(state) == "chinese" else english

def explicit_request(text):
    value = text.casefold().strip().strip(".!?。！？ ")
    match = re.fullmatch(
        r"(?:no[,，]?\s+)?(?:please\s+)?"
        r"(?:(?:can|could|would|will) you\s+|i (?:would like|want) you to\s+)?"
        r"(?:please\s+)?(?:speak|talk in|reply in|respond in|use)\s+"
        r"(english|chinese|mandarin)(?:\s+(?:please|with me|to me|from now on|only))?", value)
    if match:
        return "english" if match[1] == "english" else "chinese"
    match = re.fullmatch(r"(?:请|请你|麻烦你)?(?:说|讲|用)(英语|英文|中文|汉语)(?:说|回答|跟我说|和我说)?", value)
    if match:
        return "english" if match[1] in {"英语", "英文"} else "chinese"
    return None

def acknowledgement(value):
    return {
        "english": "Of course! I'll speak English. What would you like to talk about?",
        "chinese": "好呀，我们用中文聊。你想聊什么？",
        "bilingual": "Sure! We'll chat in English and use Chinese when it fits.",
    }[value]
