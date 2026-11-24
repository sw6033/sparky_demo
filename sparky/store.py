"""SQLite persistence for learner profiles, multi-session turns, mastery, memory & POC presets."""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Callable

from . import diagnostics, mastery, memory, segments
from .curriculum import Curriculum


class Missing(Exception):
    pass


class Conflict(Exception):
    pass


def _dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _uid() -> str:
    return uuid.uuid4().hex


def _new_session_state(session_num: int, opening_text: str, opening_cards: list[dict] | None = None) -> dict:
    reply_id = _uid()
    segs = segments.make_segments(opening_text)
    return {
        "id": _uid(),
        "session_num": session_num,
        "turn_count": 0,
        "cooldown_until_turn": 0,
        "new_targets_introduced": [],
        "dynamic_words_created": 0,
        "active_playlet": None,
        "ui_cards": opening_cards or [],
        "latest_reply": {
            "id": reply_id,
            "text": opening_text,
            "delivery": "pending",
            "segments": segs,
        },
    }


class Store:
    def __init__(self, db_path: Path | str, curriculum: Curriculum):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.curriculum = curriculum
        with self._conn() as db:
            db.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA foreign_keys=ON;
                CREATE TABLE IF NOT EXISTS profiles(
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    preset TEXT NOT NULL DEFAULT 'fresh',
                    created_at REAL NOT NULL,
                    state_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS turns(
                    id TEXT PRIMARY KEY,
                    profile_id TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
                    session_num INTEGER NOT NULL,
                    turn_index INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    user_text TEXT NOT NULL,
                    source TEXT NOT NULL,
                    reply_id TEXT NOT NULL,
                    reply_text TEXT NOT NULL,
                    move TEXT NOT NULL,
                    ui_cards_json TEXT NOT NULL,
                    meta_json TEXT NOT NULL,
                    focus_json TEXT NOT NULL,
                    delivery_json TEXT NOT NULL,
                    timing_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS turns_profile_session ON turns(profile_id, session_num, turn_index);
                CREATE TABLE IF NOT EXISTS cancellations(
                    profile_id TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
                    turn_id TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY(profile_id, turn_id)
                );
                """
            )

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def close(self) -> None:
        pass

    def list_profiles(self) -> list[dict]:
        with self._conn() as db:
            rows = db.execute("SELECT id, name, preset, created_at, state_json FROM profiles ORDER BY created_at ASC").fetchall()
        out = []
        for r in rows:
            st = json.loads(r["state_json"])
            out.append({
                "id": r["id"],
                "name": r["name"],
                "preset": r["preset"],
                "session_num": st["active_session"]["session_num"],
            })
        return out

    def create_profile(self, name: str, preset: str = "fresh") -> dict:
        clean_name = (name or "").strip()[:32] or ("Maya" if preset == "returning_dino" else "Leo")
        pid = _uid()
        if preset == "returning_dino":
            state = self._seed_returning_dino(clean_name)
        else:
            preset = "fresh"
            opening = (
                f"Hey there! I'm Sparky, your bilingual fox buddy! "
                f"What's your name, or what fun thing were you up to today?"
            )
            state = {
                "revision": 0,
                "language_mode": "bilingual",
                "mastery_ledger": {},
                "personal_lexicon": [],
                "memory": memory.initial_memory(clean_name if clean_name not in {"小朋友", "New Kid"} else ""),
                "active_session": _new_session_state(1, opening),
            }
        with self._conn() as db:
            db.execute(
                "INSERT INTO profiles(id, name, preset, created_at, state_json) VALUES(?, ?, ?, ?, ?)",
                (pid, clean_name, preset, time.time(), _dump(state)),
            )
        return self.public_state(pid)

    def _seed_returning_dino(self, name: str) -> dict:
        """Pre-seed a child entering Session 2 after completing Unit 1 & sharing dinosaur interest."""
        ledger = {
            "voc_nihao": {**mastery.default_entry(), "stage": "mastered", "exposures": 3, "uses": 3, "spontaneous_uses": 2, "last_session": 1, "last_turn": 4, "last_outcome": "spontaneous_use"},
            "voc_henhao": {**mastery.default_entry(), "stage": "practiced", "exposures": 2, "uses": 2, "spontaneous_uses": 1, "last_session": 1, "last_turn": 3, "last_outcome": "scaffolded_use"},
            "voc_pengyou": {**mastery.default_entry(), "stage": "practiced", "exposures": 2, "uses": 1, "spontaneous_uses": 0, "last_session": 1, "last_turn": 5, "last_outcome": "scaffolded_use"},
            "pat_hello_name": {**mastery.default_entry(), "stage": "practiced", "exposures": 2, "uses": 2, "spontaneous_uses": 1, "last_session": 1, "last_turn": 4, "last_outcome": "scaffolded_use"},
        }
        mem = memory.initial_memory(name)
        mem["facts"] = [
            {"id": "seed1", "text": f"{name} built a giant Lego T-Rex named Chomper", "session": 1},
            {"id": "seed2", "text": "Loves dinosaurs and space rockets", "session": 1},
        ]
        mem["episodic_summaries"] = [
            {
                "session_num": 1,
                "turn_count": 5,
                "summary": f"Met {name}, talked about building a Lego T-Rex named Chomper, and practiced our secret friend greeting in Chinese.",
                "highlights": ["Built a Lego T-Rex named Chomper", "Became 好朋友 (good friends) with Sparky"],
                "open_threads": ["Ask if Chomper the Lego T-Rex went on any new adventures today"],
                "practiced_zh": ["你好", f"你好，我是{name}", "很好", "好朋友"],
            }
        ]
        opening = (
            f"你好, {name}! Welcome back, my 好朋友! "
            f"Did Chomper your Lego T-Rex go on any wild adventures since last time?"
        )
        cards = [c for c in (self.curriculum.card_for("voc_nihao"), self.curriculum.card_for("voc_pengyou")) if c]
        return {
            "revision": 0,
            "language_mode": "bilingual",
            "mastery_ledger": ledger,
            "personal_lexicon": [],
            "memory": mem,
            "active_session": _new_session_state(2, opening, cards),
        }

    def delete_profile(self, pid: str) -> None:
        with self._conn() as db:
            cur = db.execute("DELETE FROM profiles WHERE id = ?", (pid,))
            if cur.rowcount == 0:
                raise Missing("档案不存在。")

    def _load(self, db: sqlite3.Connection, pid: str) -> tuple[sqlite3.Row, dict]:
        row = db.execute("SELECT id, name, preset, created_at, state_json FROM profiles WHERE id = ?", (pid,)).fetchone()
        if not row:
            raise Missing("档案不存在。")
        return row, json.loads(row["state_json"])

    def _save(self, db: sqlite3.Connection, pid: str, state: dict) -> None:
        db.execute("UPDATE profiles SET state_json = ? WHERE id = ?", (_dump(state), pid))

    def session_turns(self, pid: str, session_num: int | None = None, limit: int = 20) -> list[dict]:
        with self._conn() as db:
            row, state = self._load(db, pid)
            s_num = session_num if session_num is not None else state["active_session"]["session_num"]
            rows = db.execute(
                """
                SELECT id, session_num, turn_index, created_at, user_text, source,
                       reply_id, reply_text, move, ui_cards_json, meta_json, focus_json,
                       delivery_json, timing_json
                FROM turns
                WHERE profile_id = ? AND session_num = ?
                ORDER BY turn_index ASC
                """,
                (pid, s_num),
            ).fetchall()
        out = []
        for r in rows[-limit:]:
            out.append({
                "id": r["id"],
                "session_num": r["session_num"],
                "turn_index": r["turn_index"],
                "created_at": r["created_at"],
                "user_text": r["user_text"],
                "source": r["source"],
                "reply_id": r["reply_id"],
                "reply_text": r["reply_text"],
                "move": r["move"],
                "ui_cards": json.loads(r["ui_cards_json"]),
                "meta": json.loads(r["meta_json"]),
                "focus": json.loads(r["focus_json"]),
                "delivery": json.loads(r["delivery_json"]),
                "timing": json.loads(r["timing_json"]),
            })
        return out

    def recent_traces(self, pid: str, limit: int = 30) -> list[dict]:
        with self._conn() as db:
            self._load(db, pid)
            rows = db.execute(
                """
                SELECT id, session_num, turn_index, created_at, user_text, source,
                       reply_id, reply_text, move, ui_cards_json, meta_json, focus_json,
                       delivery_json, timing_json
                FROM turns
                WHERE profile_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (pid, limit),
            ).fetchall()
        return [
            {
                "id": r["id"],
                "session_num": r["session_num"],
                "turn_index": r["turn_index"],
                "created_at": r["created_at"],
                "user_text": r["user_text"],
                "source": r["source"],
                "reply_id": r["reply_id"],
                "reply_text": r["reply_text"],
                "move": r["move"],
                "ui_cards": json.loads(r["ui_cards_json"]),
                "meta": json.loads(r["meta_json"]),
                "focus": json.loads(r["focus_json"]),
                "delivery": json.loads(r["delivery_json"]),
                "timing": json.loads(r["timing_json"]),
            }
            for r in rows
        ]

    def public_state(self, pid: str) -> dict:
        with self._conn() as db:
            row, state = self._load(db, pid)
        session = state["active_session"]
        progress = mastery.syllabus_progress(
            self.curriculum,
            state.get("mastery_ledger", {}),
            state.get("personal_lexicon", []),
        )
        focus_preview = mastery.compute_session_focus(self.curriculum, deepcopy(state), "")
        turns = self.session_turns(pid, session["session_num"], limit=30)

        # Collect unlocked/practiced stars for the kid-facing Companion Screen ribbon
        stars = []
        for u in progress["units"]:
            for t in u["targets"]:
                if t["stage"] != "unseen":
                    stars.append({
                        "id": t["id"],
                        "zh": t["zh"].split("{")[0] + ("…" if "{" in t["zh"] else ""),
                        "en": t["en"],
                        "emoji": t["emoji"],
                        "stage": t["stage"],
                    })
        for d in state.get("personal_lexicon", []):
            stars.append({
                "id": d["id"],
                "zh": d["zh"],
                "en": d["en"],
                "emoji": d["emoji"],
                "stage": d.get("stage", "exposed"),
            })

        return {
            "profile": {
                "id": row["id"],
                "name": row["name"],
                "preset": row["preset"],
            },
            "revision": state["revision"],
            "language_mode": state.get("language_mode", "bilingual"),
            "session": {
                "id": session["id"],
                "session_num": session["session_num"],
                "turn_count": session["turn_count"],
                "active_playlet": session.get("active_playlet"),
                "ui_cards": session.get("ui_cards", []),
                "reply": session["latest_reply"],
            },
            "stars": stars[-12:],
            "history": [
                {
                    "turn_index": t["turn_index"],
                    "user_text": t["user_text"],
                    "reply_text": t["reply_text"],
                    "move": t["move"],
                }
                for t in turns[-12:]
            ],
            "inspector": {
                "focus": focus_preview,
                "progress": progress,
                "memory": deepcopy(state.get("memory", {})),
            },
        }

    def cancel_turn(self, pid: str, turn_id: str) -> None:
        with self._conn() as db:
            db.execute("BEGIN IMMEDIATE")
            self._load(db, pid)
            db.execute(
                "INSERT OR REPLACE INTO cancellations(profile_id, turn_id, created_at) VALUES(?, ?, ?)",
                (pid, turn_id, time.time()),
            )
            db.execute(
                """
                DELETE FROM cancellations
                WHERE profile_id = ? AND turn_id NOT IN (
                    SELECT turn_id FROM cancellations WHERE profile_id = ? ORDER BY created_at DESC LIMIT 128
                )
                """,
                (pid, pid),
            )

    def ensure_not_cancelled(self, pid: str, turn_id: str) -> None:
        with self._conn() as db:
            self._load(db, pid)
            if db.execute("SELECT 1 FROM cancellations WHERE profile_id = ? AND turn_id = ?", (pid, turn_id)).fetchone():
                raise Conflict("本轮已打断取消。")

    def prepare_turn_context(self, pid: str, turn_id: str, user_text: str) -> dict:
        with self._conn() as db:
            row, state = self._load(db, pid)
            if db.execute("SELECT 1 FROM cancellations WHERE profile_id = ? AND turn_id = ?", (pid, turn_id)).fetchone():
                raise Conflict("本轮已打断取消。")
            existing = db.execute("SELECT id FROM turns WHERE id = ? AND profile_id = ?", (turn_id, pid)).fetchone()
            if existing:
                return {"duplicate": True, "public": self.public_state(pid)}

        session_num = state["active_session"]["session_num"]
        recent_turns = self.session_turns(pid, session_num, limit=10)
        focus = mastery.compute_session_focus(self.curriculum, deepcopy(state), user_text)
        return {
            "duplicate": False,
            "profile_name": row["name"],
            "state_snapshot": state,
            "focus": focus,
            "memory": memory.context_snapshot(state.get("memory", {})),
            "recent_turns": [
                {
                    "user": t["user_text"],
                    "sparky": t["reply_text"],
                    "move": t["move"],
                    "delivery": t["delivery"].get("delivery", "complete"),
                }
                for t in recent_turns[-8:]
            ],
            "last_reply_delivery": state["active_session"]["latest_reply"],
            "hotwords": self.curriculum.asr_hotwords(
                focus["active_unit"]["id"],
                [d["zh"] for d in state.get("personal_lexicon", [])],
            ),
        }

    def commit_turn(
        self,
        pid: str,
        turn_id: str,
        user_text: str,
        source: str,
        reply_text: str,
        meta: dict,
        focus: dict,
        timing: dict | None = None,
    ) -> dict:
        reply_id = _uid()
        segs = segments.make_segments(reply_text)
        with self._conn() as db:
            db.execute("BEGIN IMMEDIATE")
            row, state = self._load(db, pid)
            if db.execute("SELECT 1 FROM cancellations WHERE profile_id = ? AND turn_id = ?", (pid, turn_id)).fetchone():
                raise Conflict("本轮已打断取消。")
            existing = db.execute("SELECT id FROM turns WHERE id = ? AND profile_id = ?", (turn_id, pid)).fetchone()
            if existing:
                return self.public_state(pid)

            ui_cards, applied_obs = mastery.apply_turn_result(
                self.curriculum,
                state,
                reply_id=reply_id,
                reply_text=reply_text,
                segments=segs,
                meta=meta,
            )
            added_facts = memory.apply_memory_notes(
                state.setdefault("memory", memory.initial_memory()),
                meta,
                state["active_session"]["session_num"],
            )
            if state["memory"].get("child_name") and row["name"] in {"Leo", "小朋友", "New Kid"}:
                db.execute("UPDATE profiles SET name = ? WHERE id = ?", (state["memory"]["child_name"], pid))

            session = state["active_session"]
            session["turn_count"] += 1
            session["ui_cards"] = ui_cards
            reply_obj = {
                "id": reply_id,
                "text": reply_text,
                "delivery": "pending",
                "segments": segs,
            }
            session["latest_reply"] = reply_obj
            state["revision"] += 1

            enriched_meta = {
                **meta,
                "applied_observations": applied_obs,
                "added_facts": added_facts,
            }
            db.execute(
                """
                INSERT INTO turns(
                    id, profile_id, session_num, turn_index, created_at,
                    user_text, source, reply_id, reply_text, move,
                    ui_cards_json, meta_json, focus_json, delivery_json, timing_json
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    turn_id,
                    pid,
                    session["session_num"],
                    session["turn_count"],
                    time.time(),
                    user_text,
                    source,
                    reply_id,
                    reply_text,
                    str(meta.get("move") or "companionship_only"),
                    _dump(ui_cards),
                    _dump(enriched_meta),
                    _dump(focus),
                    _dump(reply_obj),
                    _dump(timing or {}),
                ),
            )
            self._save(db, pid, state)
        return self.public_state(pid)

    def record_delivery(self, pid: str, reply_id: str, status: str, segment: int | None = None) -> None:
        if status not in {"started", "complete", "interrupted", "failed"}:
            return
        with self._conn() as db:
            db.execute("BEGIN IMMEDIATE")
            _, state = self._load(db, pid)
            reply_obj = state["active_session"]["latest_reply"]
            if reply_obj["id"] != reply_id:
                return
            if reply_obj["delivery"] == "complete":
                return

            segs = reply_obj.get("segments", [])
            if segment is None:
                reply_obj["delivery"] = status
                for s in segs:
                    if s["delivery"] != "complete":
                        s["delivery"] = status
            elif 0 <= segment < len(segs):
                if segs[segment]["delivery"] != "complete":
                    segs[segment]["delivery"] = status
                statuses = [s["delivery"] for s in segs]
                if all(st == "complete" for st in statuses):
                    reply_obj["delivery"] = "complete"
                elif "interrupted" in statuses:
                    reply_obj["delivery"] = "interrupted"
                elif "failed" in statuses:
                    reply_obj["delivery"] = "failed"
                elif "started" in statuses:
                    reply_obj["delivery"] = "started"

            mastery.reconcile_delivery(self.curriculum, state, reply_id, reply_obj)
            self._save(db, pid, state)
            db.execute(
                "UPDATE turns SET delivery_json = ? WHERE profile_id = ? AND reply_id = ?",
                (_dump(reply_obj), pid, reply_id),
            )

    def update_timing(self, pid: str, turn_id: str, metrics: dict) -> None:
        with self._conn() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT timing_json FROM turns WHERE id = ? AND profile_id = ?", (turn_id, pid)).fetchone()
            if not row:
                return
            merged = {**json.loads(row["timing_json"]), **metrics}
            db.execute("UPDATE turns SET timing_json = ? WHERE id = ?", (_dump(merged), turn_id))

    def start_next_session(
        self,
        pid: str,
        summarizer: Callable[[int, list[dict], dict], dict | None] | None = None,
        opening_generator: Callable[[dict, dict], tuple[str, list[str]]] | None = None,
    ) -> dict:
        """Archive current session into episodic memory and launch Session N+1."""
        with self._conn() as db:
            row, state = self._load(db, pid)
        current_session_num = state["active_session"]["session_num"]
        turns = self.session_turns(pid, current_session_num, limit=50)

        episode = None
        if turns and summarizer:
            episode = summarizer(current_session_num, turns, state.get("memory", {}))
        if not episode:
            episode = memory.fallback_consolidate_session(
                current_session_num,
                turns,
                state.get("mastery_ledger", {}),
                state.get("personal_lexicon", []),
            )

        with self._conn() as db:
            db.execute("BEGIN IMMEDIATE")
            row, state = self._load(db, pid)
            if turns:
                memory.record_session_summary(state.setdefault("memory", memory.initial_memory()), episode)

            next_num = state["active_session"]["session_num"] + 1
            preview_state = deepcopy(state)
            preview_state["active_session"] = _new_session_state(next_num, "")
            next_focus = mastery.compute_session_focus(self.curriculum, preview_state, "")

            opening_text = ""
            card_ids: list[str] = []
            if opening_generator:
                opening_text, card_ids = opening_generator(
                    memory.context_snapshot(state.get("memory", {})),
                    next_focus,
                )
            if not opening_text:
                opening_text, card_ids = self._fallback_next_opening(row["name"], state, next_focus)

            cards = [c for cid in card_ids[:2] if (c := self.curriculum.card_for(cid))]
            state["active_session"] = _new_session_state(next_num, opening_text, cards)
            state["revision"] += 1
            self._save(db, pid, state)

        return self.public_state(pid)

    def _fallback_next_opening(self, fallback_name: str, state: dict, next_focus: dict) -> tuple[str, list[str]]:
        mem = state.get("memory", {})
        name = mem.get("child_name") or fallback_name
        snowball = next_focus.get("snowball_familiar_bank", [])
        greet_zh = "你好" if any(s["zh"].startswith("你好") for s in snowball) else None
        episodes = mem.get("episodic_summaries", [])
        last_highlight = episodes[-1]["highlights"][0] if episodes and episodes[-1].get("highlights") else None

        prefix = f"{greet_zh}, {name}! " if greet_zh else f"Hey {name}, welcome back! "
        if last_highlight:
            body = f"I still remember what you shared last time ({last_highlight[:55]})! What's new with you today?"
        else:
            body = "So happy to hang out again! What fun thing happened today?"
        cards = ["voc_nihao"] if greet_zh else []
        return prefix + body, cards

    def mutate_memory(self, pid: str, op: str, value: str = "") -> dict:
        with self._conn() as db:
            db.execute("BEGIN IMMEDIATE")
            _, state = self._load(db, pid)
            mem = state.setdefault("memory", memory.initial_memory())
            if op == "add_fact" and value.strip():
                memory.apply_memory_notes(mem, {"memory_notes": [value.strip()]}, state["active_session"]["session_num"])
            elif op == "remove_fact":
                mem["facts"] = [f for f in mem.get("facts", []) if f["id"] != value]
            elif op == "set_language" and value in {"bilingual", "english", "chinese"}:
                state["language_mode"] = value
            state["revision"] += 1
            self._save(db, pid, state)
        return self.public_state(pid)
