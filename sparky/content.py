"""Load and validate curriculum, then pin both knowledge and activity rules in a run."""
import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

def norm(text):
    return re.sub(r"[\s.,!?，。！？、'’]+", "", text.casefold())

def read_group(path):
    result = {}
    for file in sorted(path.glob("*.json")):
        data = json.loads(file.read_text())
        if data["id"] in result or file.stem != data["id"] or not isinstance(data["version"], int) or data["version"] < 1:
            raise ValueError(f"内容标识或版本无效: {file}")
        result[data["id"]] = data
    if not result:
        raise ValueError(f"内容目录为空: {path}")
    return result

class Catalog:
    def __init__(self, root=ROOT / "curriculum"):
        goal_library = json.loads((root / "goals.json").read_text())
        conversation = root / "conversation.json"
        self.conversation = json.loads(conversation.read_text()) if conversation.exists() else []
        self.cue_words = set()
        for entry in self.conversation:
            assert_fields(entry, ("word","offer","follow"))
            cues = entry.get("cues")
            if not isinstance(cues,list) or not cues or any(not isinstance(c,str) or not c.strip() for c in cues):
                raise ValueError("随聊词条需要中英文触发词：" + entry["word"])
            self.cue_words.update(norm(c) for c in cues)
        templates = read_group(root / "templates")
        self.lessons = read_group(root / "lessons")
        for lesson in self.lessons.values():
            assert_fields(lesson, ("title", "icon"))
            items = lesson["items"]
            if not items or len({x["id"] for x in items}) != len(items):
                raise ValueError(f"缺少词条或词条重复: {lesson['id']}")
            seen = {}
            for item in items:
                assert_fields(item, ("id","zh","en","icon"))
                for key in ("forms","english","aliases"):
                    if not isinstance(item[key],list) or (key=="forms" and not item[key]):
                        raise ValueError(f"无效词条字段: {key}")
                    if any(not isinstance(v,str) or not v.strip() for v in item[key]):
                        raise ValueError(f"无效词条: {item['id']}")
                item["answers"]=compile_answers(lesson,item)
                for term in item["answers"]:
                    if seen.setdefault(term,item["id"]) != item["id"]:
                        raise ValueError(f"活动内词条歧义: {term}")
            lesson["goals"] = {role: {"id":ref, "label":goal_library[ref]} for role,ref in lesson["goals"].items()}
            for goal in lesson["goals"].values():
                assert_fields(goal, ("id","label"))
            lesson["rules"] = {}
            for mode in ("A","B"):
                rule = templates[lesson["activities"][mode]]
                if rule["mode"] != mode or not rule["steps"]:
                    raise ValueError(f"活动类型或步骤错误: {rule['id']}")
                policy=rule["recovery"]
                assert_fields(policy, ("unclear","wait","release"))
                if type(policy.get("clarifications")) is not int or policy["clarifications"]<0:
                    raise ValueError(f"无效澄清次数: {rule['id']}")
                for step in rule["steps"]:
                    if "expected" in step:
                        target_id=lesson.get("targets",{}).get(step["expected"])
                        if target_id not in {item["id"] for item in items} or step["items"]!="all":
                            raise ValueError(f"听辨目标无效: {lesson['id']}")
                    if (step["goal"] not in lesson["goals"] or not lesson["prompts"].get(step["prompt"])
                        or step["accept"] not in ("meaning","target") or step["items"] not in ("all","selected")
                        or not isinstance(step["remember"],bool)):
                        raise ValueError(f"活动步骤引用错误: {lesson['id']} / {rule['id']}")
                    text=lesson["prompts"][step["prompt"]]
                    if "expected" in step:
                        text.format(target_zh="示例",target_en="example")
                    elif "{" in text or "}" in text:
                        raise ValueError(f"提示语缺少目标绑定: {text}")
                lesson["rules"][mode] = rule
            lesson["digest"] = hashlib.sha256(json.dumps(lesson,sort_keys=True).encode()).hexdigest()[:16]

    def listing(self):
        return [{**{k:x[k] for k in ("id","title","icon","version")},
                 "entry_phrases":[p for p in x.get("entry_phrases",[]) if norm(p) not in self.cue_words]}
                for x in self.lessons.values()]

    def conversation_entry(self, text):
        # Match English words inside mixed-language speech, but never "red" inside "tired".
        best, length = None, 0
        for entry in self.conversation:
            for cue in entry["cues"]:
                if len(cue) <= length:
                    continue
                pattern = re.escape(cue)
                if cue.isascii():
                    pattern = r"(?<![a-z])" + pattern + r"(?![a-z])"
                if re.search(pattern,text,re.I):
                    best, length = entry, len(cue)
        return best

def assert_fields(obj, fields):
    if any(not isinstance(obj.get(k),str) or not obj[k].strip() for k in fields):
        raise ValueError(f"缺少必需文字字段: {fields}")



def compile_answers(lesson,item):
    english=[item["en"],*item["english"]]
    terms={"target":list(item["forms"]), "word":[item["zh"],*item["aliases"]],
           "english":english, "mixed":[]}
    for key,kind in (("target_frames","target"),("meaning_frames","mixed")):
        for frame in lesson.get(key,[]):
            if "{zh}" not in frame and "{en}" not in frame:
                raise ValueError(f"缺少词条占位符: {frame}")
            for word in english:
                terms[kind].append(frame.format(zh=item["zh"],en=word))
    answers={}
    for kind,phrases in terms.items():
        for phrase in phrases:
            answers.setdefault(norm(phrase),kind)
    return answers
