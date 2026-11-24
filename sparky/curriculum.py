"""Configurable, hot-reloadable curriculum loader & validator (pedagogy + syllabus)."""
from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from pathlib import Path
from threading import RLock

ROOT = Path(__file__).resolve().parent.parent
SLOT_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def _require_str(obj: dict, key: str, context: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{context} 缺少有效字符串字段: {key}")
    return value.strip()


def _require_int(obj: dict, key: str, minimum: int, context: str) -> int:
    value = obj.get(key)
    if type(value) is not int or value < minimum:
        raise ValueError(f"{context} 字段 {key} 必须是 >= {minimum} 的整数")
    return value


class Curriculum:
    """Thread-safe holder of validated pedagogy rules and ordered syllabus units."""

    def __init__(self, directory: Path | str = ROOT / "curriculum"):
        self.directory = Path(directory)
        self._lock = RLock()
        self.reload()

    def reload(self) -> dict:
        pedagogy_path = self.directory / "pedagogy.json"
        syllabus_path = self.directory / "syllabus.json"
        if not pedagogy_path.is_file() or not syllabus_path.is_file():
            raise ValueError(f"课程目录缺少 pedagogy.json 或 syllabus.json: {self.directory}")

        pedagogy = json.loads(pedagogy_path.read_text(encoding="utf-8"))
        syllabus = json.loads(syllabus_path.read_text(encoding="utf-8"))
        compiled = self._validate_and_compile(pedagogy, syllabus)

        with self._lock:
            self.pedagogy = compiled["pedagogy"]
            self.syllabus = compiled["syllabus"]
            self.stages = compiled["stages"]
            self.units = compiled["units"]
            self.unit_map = compiled["unit_map"]
            self.targets = compiled["targets"]
            self.playlets = compiled["playlets"]
            self.digest = compiled["digest"]
        return self.summary()

    @staticmethod
    def _validate_and_compile(pedagogy: dict, syllabus: dict) -> dict:
        if not isinstance(pedagogy, dict) or not isinstance(syllabus, dict):
            raise ValueError("课程配置顶层必须是 JSON 对象")
        _require_int(pedagogy, "version", 1, "pedagogy.json")
        _require_str(pedagogy, "target_age_band", "pedagogy.json")

        rhythm = pedagogy.get("session_rhythm")
        if not isinstance(rhythm, dict):
            raise ValueError("pedagogy.json 缺少 session_rhythm")
        for key, low in (
            ("warmup_turns", 0),
            ("max_new_targets_per_session", 1),
            ("bridge_interval_turns", 1),
            ("cooldown_after_decline_turns", 1),
            ("max_dynamic_personal_words_per_session", 0),
            ("max_snowball_items_in_context", 1),
        ):
            _require_int(rhythm, key, low, "session_rhythm")

        stages = pedagogy.get("mastery_stages")
        if not isinstance(stages, list) or len(stages) < 3 or len(set(stages)) != len(stages):
            raise ValueError("mastery_stages 必须是不重复的有序列表")
        if stages[0] != "unseen" or stages[-1] != "mastered":
            raise ValueError("mastery_stages 必须以 unseen 开始、以 mastered 结束")

        moves = pedagogy.get("teaching_moves")
        if not isinstance(moves, dict) or not moves:
            raise ValueError("pedagogy.json 缺少 teaching_moves")
        for move_id in moves:
            _require_str(moves, move_id, "teaching_moves")

        ladder = pedagogy.get("mastery_ladder")
        if not isinstance(ladder, dict) or set(ladder.keys()) != set(stages):
            raise ValueError("mastery_ladder 必须精确覆盖所有 mastery_stages")
        for stage_id, spec in ladder.items():
            if not isinstance(spec, dict):
                raise ValueError(f"mastery_ladder.{stage_id} 无效")
            _require_str(spec, "label", f"mastery_ladder.{stage_id}")
            _require_str(spec, "goal", f"mastery_ladder.{stage_id}")
            allowed = spec.get("allowed_moves")
            if not isinstance(allowed, list) or not allowed or any(m not in moves for m in allowed):
                raise ValueError(f"mastery_ladder.{stage_id}.allowed_moves 引用了未定义的教学动作")

        principles = pedagogy.get("scaffolding_principles")
        if not isinstance(principles, list) or not principles or any(not isinstance(x, str) or not x.strip() for x in principles):
            raise ValueError("pedagogy.json 缺少有效的 scaffolding_principles")

        _require_int(syllabus, "version", 1, "syllabus.json")
        _require_str(syllabus, "title", "syllabus.json")
        raw_units = syllabus.get("units")
        if not isinstance(raw_units, list) or not raw_units:
            raise ValueError("syllabus.json 必须包含至少一个单元")

        units = sorted(deepcopy(raw_units), key=lambda u: u.get("order", 0))
        unit_map: dict[str, dict] = {}
        targets: dict[str, dict] = {}
        playlets: dict[str, dict] = {}
        orders_seen: set[int] = set()

        for unit in units:
            if not isinstance(unit, dict):
                raise ValueError("单元必须是对象")
            uid = _require_str(unit, "id", "unit")
            if uid in unit_map:
                raise ValueError(f"重复的单元 ID: {uid}")
            order = _require_int(unit, "order", 1, f"unit[{uid}]")
            if order in orders_seen:
                raise ValueError(f"重复的单元顺序 order={order}: {uid}")
            orders_seen.add(order)
            _require_str(unit, "title", f"unit[{uid}]")
            _require_str(unit, "communicative_goal", f"unit[{uid}]")

            bridges = unit.get("topic_bridges")
            if not isinstance(bridges, list) or not bridges or any(not isinstance(b, str) or not b.strip() for b in bridges):
                raise ValueError(f"unit[{uid}] 需要非空的 topic_bridges")

            unlock = unit.get("unlock_next")
            if not isinstance(unlock, dict):
                raise ValueError(f"unit[{uid}] 缺少 unlock_next")
            _require_int(unlock, "min_patterns_practiced", 0, f"unit[{uid}].unlock_next")
            _require_int(unlock, "min_vocab_practiced", 0, f"unit[{uid}].unlock_next")

            patterns = unit.get("patterns")
            vocab = unit.get("vocabulary")
            if not isinstance(patterns, list) or not patterns:
                raise ValueError(f"unit[{uid}] 需要至少一个功能句块 (patterns)")
            if not isinstance(vocab, list) or not vocab:
                raise ValueError(f"unit[{uid}] 需要至少一个锚点词汇 (vocabulary)")

            for pat in patterns:
                pid = _require_str(pat, "id", f"unit[{uid}].pattern")
                if pid in targets:
                    raise ValueError(f"全局教学目标 ID 重复: {pid}")
                zh = _require_str(pat, "zh", f"pattern[{pid}]")
                _require_str(pat, "pinyin", f"pattern[{pid}]")
                _require_str(pat, "en", f"pattern[{pid}]")
                slots = SLOT_RE.findall(zh)
                if pat.get("slot") and not slots:
                    raise ValueError(f"pattern[{pid}] 声明了 slot 但 zh 模板未包含占位符: {zh}")
                scaffolds = pat.get("scaffolds")
                if not isinstance(scaffolds, dict) or not scaffolds.get("expose"):
                    raise ValueError(f"pattern[{pid}] 需要 scaffolds.expose")
                examples = pat.get("examples", [])
                if not isinstance(examples, list) or not examples:
                    raise ValueError(f"pattern[{pid}] 需要 examples 列表")
                targets[pid] = {
                    "id": pid,
                    "kind": "pattern",
                    "unit_id": uid,
                    "zh": zh,
                    "pinyin": pat["pinyin"],
                    "en": pat["en"],
                    "emoji": pat.get("emoji", "💬"),
                    "slot": pat.get("slot"),
                    "examples": examples,
                    "scaffolds": scaffolds,
                }

            for item in vocab:
                vid = _require_str(item, "id", f"unit[{uid}].vocab")
                if vid in targets:
                    raise ValueError(f"全局教学目标 ID 重复: {vid}")
                zh = _require_str(item, "zh", f"vocab[{vid}]")
                _require_str(item, "pinyin", f"vocab[{vid}]")
                _require_str(item, "en", f"vocab[{vid}]")
                _require_str(item, "emoji", f"vocab[{vid}]")
                _require_str(item, "hook", f"vocab[{vid}]")
                targets[vid] = {
                    "id": vid,
                    "kind": "vocab",
                    "unit_id": uid,
                    "zh": zh,
                    "pinyin": item["pinyin"],
                    "en": item["en"],
                    "emoji": item["emoji"],
                    "hook": item["hook"],
                }

            unit_map[uid] = unit

        # Validate prerequisite graph (existence + DAG check) and playlet target references
        for uid, unit in unit_map.items():
            prereqs = unit.get("prerequisites", [])
            if not isinstance(prereqs, list) or any(p not in unit_map for p in prereqs):
                raise ValueError(f"unit[{uid}] 的 prerequisites 包含未知单元")
            for play in unit.get("playlets", []):
                pl_id = _require_str(play, "id", f"unit[{uid}].playlet")
                if pl_id in playlets:
                    raise ValueError(f"重复的 playlet ID: {pl_id}")
                _require_str(play, "title", f"playlet[{pl_id}]")
                _require_str(play, "setup_brief", f"playlet[{pl_id}]")
                tids = play.get("target_ids")
                if not isinstance(tids, list) or not tids or any(t not in targets for t in tids):
                    raise ValueError(f"playlet[{pl_id}] 引用了不存在的 target_ids")
                playlets[pl_id] = {**play, "unit_id": uid}

        visited: dict[str, int] = {}

        def visit(node: str) -> None:
            state = visited.get(node, 0)
            if state == 1:
                raise ValueError(f"单元 prerequisites 存在循环依赖: {node}")
            if state == 2:
                return
            visited[node] = 1
            for dep in unit_map[node].get("prerequisites", []):
                visit(dep)
            visited[node] = 2

        for uid in unit_map:
            visit(uid)

        canonical = json.dumps({"pedagogy": pedagogy, "syllabus": syllabus}, sort_keys=True, ensure_ascii=False)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
        return {
            "pedagogy": pedagogy,
            "syllabus": syllabus,
            "stages": stages,
            "units": units,
            "unit_map": unit_map,
            "targets": targets,
            "playlets": playlets,
            "digest": digest,
        }

    def stage_index(self, stage: str) -> int:
        with self._lock:
            return self.stages.index(stage) if stage in self.stages else 0

    def target(self, target_id: str) -> dict | None:
        with self._lock:
            item = self.targets.get(target_id)
            return deepcopy(item) if item else None

    def unit(self, unit_id: str) -> dict | None:
        with self._lock:
            u = self.unit_map.get(unit_id)
            return deepcopy(u) if u else None

    def playlet(self, playlet_id: str) -> dict | None:
        with self._lock:
            p = self.playlets.get(playlet_id)
            return deepcopy(p) if p else None

    def card_for(self, target_id: str) -> dict | None:
        with self._lock:
            t = self.targets.get(target_id)
            if not t:
                return None
            zh_display = t["examples"][0] if t["kind"] == "pattern" else t["zh"]
            return {
                "id": t["id"],
                "kind": t["kind"],
                "zh": zh_display,
                "template": t["zh"] if t["kind"] == "pattern" else None,
                "pinyin": t["pinyin"],
                "en": t["en"],
                "emoji": t["emoji"],
                "hook": t.get("hook") or t.get("scaffolds", {}).get("expose", ""),
            }

    def asr_hotwords(self, unit_id: str | None = None, extra_words: list[str] | None = None) -> list[str]:
        with self._lock:
            words: list[str] = []
            source_units = [self.unit_map[unit_id]] if unit_id and unit_id in self.unit_map else self.units[:2]
            for u in source_units:
                for v in u["vocabulary"]:
                    words.append(v["zh"])
                for p in u["patterns"]:
                    words.extend(p.get("examples", [])[:1])
            if extra_words:
                words.extend(extra_words)
            seen: set[str] = set()
            deduped: list[str] = []
            for w in words:
                if w and w not in seen:
                    seen.add(w)
                    deduped.append(w)
            return deduped[:16]

    def summary(self) -> dict:
        with self._lock:
            return {
                "title": self.syllabus["title"],
                "digest": self.digest,
                "age_band": self.pedagogy["target_age_band"],
                "stages": list(self.stages),
                "rhythm": deepcopy(self.pedagogy["session_rhythm"]),
                "units": [
                    {
                        "id": u["id"],
                        "order": u["order"],
                        "title": u["title"],
                        "title_zh": u.get("title_zh", u["title"]),
                        "goal": u["communicative_goal"],
                        "prerequisites": list(u.get("prerequisites", [])),
                        "patterns": [
                            {"id": p["id"], "zh": p["zh"], "pinyin": p["pinyin"], "en": p["en"], "emoji": p.get("emoji", "💬")}
                            for p in u["patterns"]
                        ],
                        "vocabulary": [
                            {"id": v["id"], "zh": v["zh"], "pinyin": v["pinyin"], "en": v["en"], "emoji": v["emoji"], "hook": v["hook"]}
                            for v in u["vocabulary"]
                        ],
                        "playlets": [
                            {"id": pl["id"], "title": pl["title"], "title_zh": pl.get("title_zh", pl["title"])}
                            for pl in u.get("playlets", [])
                        ],
                    }
                    for u in self.units
                ],
            }
