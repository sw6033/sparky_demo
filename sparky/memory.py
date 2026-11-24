"""Companion relationship memory, fact notes, and cross-session consolidation."""
from __future__ import annotations

import uuid
from copy import deepcopy


def initial_memory(child_name: str = "") -> dict:
    return {
        "child_name": child_name.strip(),
        "facts": [],
        "episodic_summaries": [],
    }


def apply_memory_notes(memory: dict, meta: dict, session_num: int) -> list[dict]:
    """Merge new child facts or name discovered during conversation."""
    added: list[dict] = []
    discovered_name = meta.get("child_name")
    if isinstance(discovered_name, str) and 1 <= len(discovered_name.strip()) <= 24:
        clean_name = discovered_name.strip()
        if clean_name.lower() not in {"sparky", "unknown", "child", "kid"}:
            memory["child_name"] = clean_name

    notes = meta.get("memory_notes")
    if not isinstance(notes, list):
        return added
    existing_norm = {f["text"].strip().casefold() for f in memory.get("facts", [])}
    for note in notes[:3]:
        text = str(note).strip() if isinstance(note, (str, int)) else str(note.get("text", "")).strip() if isinstance(note, dict) else ""
        if not text or len(text) > 120 or text.casefold() in existing_norm:
            continue
        existing_norm.add(text.casefold())
        item = {
            "id": uuid.uuid4().hex[:10],
            "text": text,
            "session": session_num,
        }
        memory.setdefault("facts", []).append(item)
        added.append(item)
    memory["facts"] = memory.get("facts", [])[-16:]
    return added


def fallback_consolidate_session(
    session_num: int,
    turns: list[dict],
    ledger: dict,
    personal_lexicon: list[dict],
) -> dict:
    """Deterministic session summary when offline or if model summary is skipped."""
    user_lines = [t["user_text"].strip() for t in turns if t.get("user_text", "").strip()]
    practiced_zh = []
    for t in turns:
        meta = t.get("meta") or {}
        for obs in meta.get("applied_observations", []):
            zh = obs.get("zh")
            if zh and zh not in practiced_zh:
                practiced_zh.append(zh)
    for dyn in personal_lexicon:
        if dyn.get("session_added") == session_num and dyn["zh"] not in practiced_zh:
            practiced_zh.append(dyn["zh"])

    if user_lines:
        highlights = user_lines[:2]
        summary_text = f"Chatted for {len(turns)} turn(s). Shared: {' / '.join(h[:60] for h in highlights)}."
    else:
        highlights = []
        summary_text = "Opened a quick visit with Sparky."

    return {
        "session_num": session_num,
        "turn_count": len(turns),
        "summary": summary_text[:200],
        "highlights": [h[:100] for h in highlights],
        "open_threads": [],
        "practiced_zh": practiced_zh[:8],
    }


def record_session_summary(memory: dict, episode: dict) -> None:
    episodes = memory.setdefault("episodic_summaries", [])
    episodes = [e for e in episodes if e.get("session_num") != episode.get("session_num")]
    episodes.append(deepcopy(episode))
    memory["episodic_summaries"] = episodes[-6:]


def context_snapshot(memory: dict) -> dict:
    return {
        "child_name": memory.get("child_name") or None,
        "confirmed_facts": [f["text"] for f in memory.get("facts", [])[-8:]],
        "past_sessions": deepcopy(memory.get("episodic_summaries", [])[-3:]),
    }
