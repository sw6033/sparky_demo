"""Mastery state machine, pedagogical invariants, and per-turn SessionFocus planner."""
from __future__ import annotations

import re
import time
from copy import deepcopy
from .curriculum import Curriculum

OUTCOMES = {"exposed", "comprehended", "imitated", "scaffolded_use", "spontaneous_use", "declined"}


def default_entry() -> dict:
    return {
        "stage": "unseen",
        "exposures": 0,
        "uses": 0,
        "spontaneous_uses": 0,
        "last_session": 0,
        "last_turn": 0,
        "last_outcome": None,
        "pre_exposure_stage": None,
        "pending_exposure_reply": None,
        "pending_exposure_segments": [],
        "modeled_in_reply": None,
    }


def _zh_stem(zh_template: str) -> str:
    """Extract fixed Chinese stem from a pattern template like '我喜欢{item}' -> '我喜欢'."""
    cleaned = re.sub(r"\{[a-zA-Z0-9_]+\}", "", zh_template)
    cleaned = re.sub(r"[，。！？,.!?\s]+", "", cleaned)
    return cleaned


def target_appears_in_text(target: dict, text: str) -> bool:
    if not text:
        return False
    compact = re.sub(r"\s+", "", text)
    if target["kind"] == "vocab":
        # Match full zh or individual items in grouped vocab like '一、二、三'
        if target["zh"] in compact:
            return True
        parts = [p.strip() for p in target["zh"].split("、") if p.strip()]
        return len(parts) > 1 and any(p in compact for p in parts)
    stem = _zh_stem(target["zh"])
    return bool(stem and len(stem) >= 2 and stem in compact)


def unit_unlocked(curriculum: Curriculum, unit: dict, ledger: dict) -> bool:
    for prereq_id in unit.get("prerequisites", []):
        prereq = curriculum.unit(prereq_id)
        if prereq and not unit_completed(curriculum, prereq, ledger):
            return False
    return True


def unit_completed(curriculum: Curriculum, unit: dict, ledger: dict) -> bool:
    practiced_idx = curriculum.stage_index("practiced")
    req = unit.get("unlock_next", {})
    pat_done = sum(
        1
        for p in unit["patterns"]
        if curriculum.stage_index(ledger.get(p["id"], {}).get("stage", "unseen")) >= practiced_idx
    )
    voc_done = sum(
        1
        for v in unit["vocabulary"]
        if curriculum.stage_index(ledger.get(v["id"], {}).get("stage", "unseen")) >= practiced_idx
    )
    return pat_done >= req.get("min_patterns_practiced", 1) and voc_done >= req.get("min_vocab_practiced", 2)


def select_active_unit(curriculum: Curriculum, ledger: dict) -> dict:
    units = curriculum.summary()["units"]
    for u_summary in units:
        u = curriculum.unit(u_summary["id"])
        if not u:
            continue
        if unit_unlocked(curriculum, u, ledger) and not unit_completed(curriculum, u, ledger):
            return u
    # All units completed unlock thresholds: pick earliest unit with unmastered items, else last unit
    mastered_idx = curriculum.stage_index("mastered")
    for u_summary in units:
        u = curriculum.unit(u_summary["id"])
        if not u:
            continue
        all_ids = [p["id"] for p in u["patterns"]] + [v["id"] for v in u["vocabulary"]]
        if any(curriculum.stage_index(ledger.get(tid, {}).get("stage", "unseen")) < mastered_idx for tid in all_ids):
            return u
    return curriculum.unit(units[-1]["id"])


def syllabus_progress(curriculum: Curriculum, ledger: dict, personal_lexicon: list[dict]) -> dict:
    active = select_active_unit(curriculum, ledger)
    units_out = []
    for u_sum in curriculum.summary()["units"]:
        u = curriculum.unit(u_sum["id"])
        unlocked = unit_unlocked(curriculum, u, ledger)
        completed = unit_completed(curriculum, u, ledger)
        items = []
        for p in u["patterns"]:
            entry = ledger.get(p["id"], default_entry())
            items.append({
                "id": p["id"],
                "kind": "pattern",
                "zh": p["zh"],
                "pinyin": p["pinyin"],
                "en": p["en"],
                "emoji": p.get("emoji", "💬"),
                "stage": entry["stage"],
                "exposures": entry["exposures"],
                "uses": entry["uses"],
            })
        for v in u["vocabulary"]:
            entry = ledger.get(v["id"], default_entry())
            items.append({
                "id": v["id"],
                "kind": "vocab",
                "zh": v["zh"],
                "pinyin": v["pinyin"],
                "en": v["en"],
                "emoji": v["emoji"],
                "stage": entry["stage"],
                "exposures": entry["exposures"],
                "uses": entry["uses"],
            })
        units_out.append({
            "id": u["id"],
            "order": u["order"],
            "title": u["title"],
            "title_zh": u.get("title_zh", u["title"]),
            "goal": u["communicative_goal"],
            "unlocked": unlocked,
            "completed": completed,
            "active": u["id"] == active["id"],
            "targets": items,
        })
    return {
        "digest": curriculum.digest,
        "stages": curriculum.stages,
        "active_unit_id": active["id"],
        "units": units_out,
        "personal_lexicon": deepcopy(personal_lexicon),
    }


def compute_session_focus(
    curriculum: Curriculum,
    profile_state: dict,
    user_text: str = "",
) -> dict:
    """Deterministically compute the pedagogical slice injected into the LLM for this turn."""
    rhythm = curriculum.pedagogy["session_rhythm"]
    ladder = curriculum.pedagogy["mastery_ladder"]
    ledger: dict = profile_state.setdefault("mastery_ledger", {})
    personal_lexicon: list[dict] = profile_state.setdefault("personal_lexicon", [])
    session: dict = profile_state["active_session"]

    turn_idx = session.get("turn_count", 0)
    session_num = session.get("session_num", 1)
    cooldown_until = session.get("cooldown_until_turn", 0)
    new_introduced: list[str] = session.get("new_targets_introduced", [])
    lang_mode = profile_state.get("language_mode", "bilingual")

    explicit_learn = bool(
        re.search(r"\b(chinese|mandarin|how do (?:i|you) say|teach me|play a game)\b", user_text.lower())
        or any(k in user_text for k in ("中文", "怎么说", "教我", "玩游戏", "暗号"))
    )

    if lang_mode == "english" and not explicit_learn:
        rhythm_mode = "english_only_requested"
    elif session.get("active_playlet"):
        rhythm_mode = "playlet_active"
    elif turn_idx < cooldown_until and not explicit_learn:
        rhythm_mode = "cooldown_companionship"
    elif turn_idx < rhythm["warmup_turns"] and not explicit_learn:
        rhythm_mode = "warmup_companionship"
    else:
        rhythm_mode = "bridge_ready"

    active_unit = select_active_unit(curriculum, ledger)
    budget_left = max(0, rhythm["max_new_targets_per_session"] - len(new_introduced))

    def pick_from(candidates: list[dict], limit: int) -> list[dict]:
        # Prioritize in-progress (exposed/scaffolded) before unseen or already practiced
        def priority(item: dict) -> tuple[int, int]:
            st = ledger.get(item["id"], {}).get("stage", "unseen")
            order_map = {"exposed": 0, "scaffolded": 1, "unseen": 2, "practiced": 3, "mastered": 4}
            return (order_map.get(st, 5), curriculum.stage_index(st))

        chosen = []
        for item in sorted(candidates, key=priority):
            st = ledger.get(item["id"], {}).get("stage", "unseen")
            if st == "mastered":
                continue
            if st == "unseen" and budget_left <= 0 and item["id"] not in new_introduced and not explicit_learn:
                continue
            spec = curriculum.target(item["id"])
            stage_cfg = ladder[st]
            chosen.append({
                "id": item["id"],
                "kind": spec["kind"],
                "zh": spec["zh"],
                "pinyin": spec["pinyin"],
                "en": spec["en"],
                "emoji": spec["emoji"],
                "hook": spec.get("hook"),
                "scaffolds": spec.get("scaffolds"),
                "current_stage": st,
                "allowed_moves": stage_cfg["allowed_moves"],
                "stage_goal": stage_cfg["goal"],
            })
            if len(chosen) >= limit:
                break
        return chosen

    primary_targets: list[dict] = []
    if rhythm_mode in {"bridge_ready", "playlet_active"} or explicit_learn:
        primary_targets.extend(pick_from(active_unit["patterns"], 1))
        primary_targets.extend(pick_from(active_unit["vocabulary"], 2))

    # Build Snowball Bank: practiced/mastered items + earlier-session items ready for natural recall
    practiced_idx = curriculum.stage_index("practiced")
    snowball: list[dict] = []
    for tid, entry in ledger.items():
        st_idx = curriculum.stage_index(entry.get("stage", "unseen"))
        spec = curriculum.target(tid)
        if not spec:
            continue
        if st_idx >= practiced_idx or (st_idx >= 1 and entry.get("last_session", 0) < session_num):
            snowball.append({
                "id": tid,
                "zh": spec["zh"],
                "pinyin": spec["pinyin"],
                "en": spec["en"],
                "stage": entry["stage"],
                "cross_session_review": entry.get("last_session", 0) < session_num and st_idx < curriculum.stage_index("mastered"),
            })
    for dyn in personal_lexicon:
        snowball.append({
            "id": dyn["id"],
            "zh": dyn["zh"],
            "pinyin": dyn["pinyin"],
            "en": dyn["en"],
            "stage": dyn.get("stage", "exposed"),
            "personal_interest": True,
        })
    snowball = snowball[: rhythm["max_snowball_items_in_context"]]

    # Suggest a playlet only if at least 1 target in active_unit is >= exposed
    any_exposed = any(
        curriculum.stage_index(ledger.get(t["id"], {}).get("stage", "unseen")) >= 1
        for t in (*active_unit["patterns"], *active_unit["vocabulary"])
    )
    playlets = active_unit.get("playlets", [])
    suggested_playlet = None
    if session.get("active_playlet"):
        suggested_playlet = session["active_playlet"]
    elif rhythm_mode == "bridge_ready" and any_exposed and playlets:
        pl = playlets[0]
        suggested_playlet = {
            "id": pl["id"],
            "title": pl["title"],
            "setup_brief": pl["setup_brief"],
            "when_to_invite": pl.get("when_to_invite", ""),
            "target_ids": pl["target_ids"],
        }

    dyn_count = session.get("dynamic_words_created", 0)
    can_create_dynamic = dyn_count < rhythm["max_dynamic_personal_words_per_session"] and rhythm_mode not in {
        "english_only_requested",
        "cooldown_companionship",
    }

    return {
        "session_num": session_num,
        "turn_index": turn_idx + 1,
        "language_mode": lang_mode,
        "rhythm_mode": rhythm_mode,
        "active_unit": {
            "id": active_unit["id"],
            "title": active_unit["title"],
            "goal": active_unit["communicative_goal"],
            "topic_bridges": active_unit["topic_bridges"],
        },
        "primary_targets": primary_targets,
        "snowball_familiar_bank": snowball,
        "playlet": suggested_playlet,
        "allow_dynamic_interest_vocab": can_create_dynamic,
        "scaffolding_principles": curriculum.pedagogy["scaffolding_principles"],
    }


def _promote(curriculum: Curriculum, current_stage: str, target_stage: str) -> str:
    if curriculum.stage_index(target_stage) > curriculum.stage_index(current_stage):
        return target_stage
    return current_stage


def apply_turn_result(
    curriculum: Curriculum,
    profile_state: dict,
    reply_id: str,
    reply_text: str,
    segments: list[dict],
    meta: dict,
) -> tuple[list[dict], list[dict]]:
    """Apply validated META observations, UI cards, dynamic vocab, and invariants.

    Returns (ui_cards, applied_observations).
    """
    rhythm = curriculum.pedagogy["session_rhythm"]
    ledger: dict = profile_state.setdefault("mastery_ledger", {})
    personal_lexicon: list[dict] = profile_state.setdefault("personal_lexicon", [])
    session: dict = profile_state["active_session"]
    session_num = session["session_num"]
    turn_idx = session["turn_count"] + 1
    prev_reply = session.get("latest_reply") or {}
    prev_reply_id = prev_reply.get("id")
    prev_interrupted = prev_reply.get("delivery") in {"interrupted", "failed"}

    applied_obs: list[dict] = []

    # 1. Language mode changes if explicitly requested
    lang_pref = meta.get("language_mode")
    if lang_pref in {"bilingual", "english", "chinese"}:
        profile_state["language_mode"] = lang_pref

    # 2. Dynamic personal interest vocabulary (strictly bounded & validated)
    new_dyn = meta.get("new_personal_vocab")
    if isinstance(new_dyn, dict) and session.get("dynamic_words_created", 0) < rhythm["max_dynamic_personal_words_per_session"]:
        zh = str(new_dyn.get("zh", "")).strip()[:12]
        pinyin = str(new_dyn.get("pinyin", "")).strip()[:40]
        en = str(new_dyn.get("en", "")).strip()[:32]
        emoji = str(new_dyn.get("emoji", "✨")).strip()[:4] or "✨"
        hook = str(new_dyn.get("hook", "")).strip()[:100]
        if zh and pinyin and en and any("\u3400" <= c <= "\u9fff" for c in zh):
            existing = next((x for x in personal_lexicon if x["zh"] == zh), None)
            if not existing:
                dyn_id = f"dyn_{len(personal_lexicon) + 1}_{ int(time.time()) % 10000 }"
                existing = {
                    "id": dyn_id,
                    "kind": "personal_vocab",
                    "zh": zh,
                    "pinyin": pinyin,
                    "en": en,
                    "emoji": emoji,
                    "hook": hook or f"Your special word for {en}!",
                    "stage": "exposed",
                    "session_added": session_num,
                    "uses": 0,
                }
                personal_lexicon.append(existing)
                personal_lexicon[:] = personal_lexicon[-24:]
                session["dynamic_words_created"] = session.get("dynamic_words_created", 0) + 1

    # 3. Process child observations from META with anti-skip invariants
    raw_obs = meta.get("observations") if isinstance(meta.get("observations"), list) else []
    declined_any = False
    for obs in raw_obs[:6]:
        if not isinstance(obs, dict):
            continue
        tid = str(obs.get("target_id", "")).strip()
        outcome = str(obs.get("outcome", "")).strip()
        if outcome not in OUTCOMES:
            continue

        if outcome == "declined":
            declined_any = True
            applied_obs.append({"target_id": tid or "general", "outcome": "declined", "effective_outcome": "declined"})
            continue

        # Check personal lexicon target first
        dyn_item = next((d for d in personal_lexicon if d["id"] == tid or d["zh"] == tid), None)
        if dyn_item:
            if outcome in {"imitated", "scaffolded_use", "spontaneous_use", "comprehended"}:
                dyn_item["uses"] = dyn_item.get("uses", 0) + 1
                if outcome == "spontaneous_use":
                    dyn_item["stage"] = "mastered"
                elif dyn_item.get("stage") == "exposed":
                    dyn_item["stage"] = "practiced"
                applied_obs.append({"target_id": dyn_item["id"], "zh": dyn_item["zh"], "outcome": outcome, "stage": dyn_item["stage"]})
            continue

        spec = curriculum.target(tid)
        if not spec:
            continue
        entry = ledger.setdefault(tid, default_entry())
        before_stage = entry["stage"]
        effective = outcome

        # Invariant: If Sparky modeled this exact target in the immediately preceding turn
        # (and that reply wasn't aborted), cap spontaneous_use -> imitated so echoing never skips stages!
        just_modeled = bool(prev_reply_id and entry.get("modeled_in_reply") == prev_reply_id and not prev_interrupted)
        if effective == "spontaneous_use" and just_modeled:
            effective = "imitated"

        if effective == "exposed":
            pass  # Handled uniformly below via reply text + segment inspection
        elif effective in {"comprehended", "imitated"}:
            entry["uses"] += 1
            entry["stage"] = _promote(curriculum, before_stage, "scaffolded")
        elif effective == "scaffolded_use":
            entry["uses"] += 1
            if curriculum.stage_index(before_stage) >= curriculum.stage_index("exposed"):
                entry["stage"] = _promote(curriculum, before_stage, "practiced")
            else:
                entry["stage"] = _promote(curriculum, before_stage, "scaffolded")
        elif effective == "spontaneous_use":
            entry["uses"] += 1
            entry["spontaneous_uses"] += 1
            cross_session = entry.get("last_session", 0) > 0 and entry.get("last_session", 0) < session_num
            if cross_session or curriculum.stage_index(before_stage) >= curriculum.stage_index("practiced"):
                entry["stage"] = _promote(curriculum, before_stage, "mastered")
            elif curriculum.stage_index(before_stage) >= curriculum.stage_index("scaffolded"):
                entry["stage"] = _promote(curriculum, before_stage, "practiced")
            else:
                entry["stage"] = _promote(curriculum, before_stage, "practiced")

        if effective != "exposed":
            entry["last_session"] = session_num
            entry["last_turn"] = turn_idx
            entry["last_outcome"] = effective
            applied_obs.append({
                "target_id": tid,
                "zh": spec["zh"],
                "outcome": outcome,
                "effective_outcome": effective,
                "before_stage": before_stage,
                "after_stage": entry["stage"],
            })

    if declined_any or meta.get("move") == "cooldown":
        session["active_playlet"] = None
        session["cooldown_until_turn"] = turn_idx + rhythm["cooldown_after_decline_turns"]

    # 4. Track what Sparky modeled/exposed in THIS reply (for delivery tracking & next-turn echo guard)
    for tid, spec in curriculum.targets.items():
        mentioned = target_appears_in_text(spec, reply_text) or any(
            isinstance(o, dict) and o.get("target_id") == tid and o.get("outcome") == "exposed"
            for o in raw_obs
        )
        if not mentioned:
            continue
        entry = ledger.setdefault(tid, default_entry())
        entry["modeled_in_reply"] = reply_id
        entry["exposures"] += 1
        seg_indices = [s["index"] for s in segments if target_appears_in_text(spec, s["text"])]
        if entry["stage"] == "unseen":
            entry["pre_exposure_stage"] = "unseen"
            entry["stage"] = "exposed"
            entry["pending_exposure_reply"] = reply_id
            entry["pending_exposure_segments"] = seg_indices
            if tid not in session.setdefault("new_targets_introduced", []):
                session["new_targets_introduced"].append(tid)
            entry["last_session"] = session_num
            entry["last_turn"] = turn_idx
            entry["last_outcome"] = "exposed"
            applied_obs.append({
                "target_id": tid,
                "zh": spec["zh"],
                "outcome": "exposed",
                "effective_outcome": "exposed",
                "before_stage": "unseen",
                "after_stage": "exposed",
            })

    # 5. Playlet lifecycle
    playlet_op = meta.get("playlet_state")
    if playlet_op == "end" or declined_any:
        session["active_playlet"] = None
    elif isinstance(playlet_op, str) and curriculum.playlet(playlet_op):
        pl = curriculum.playlet(playlet_op)
        current = session.get("active_playlet")
        if current and current.get("id") == pl["id"]:
            current["turns_played"] = current.get("turns_played", 1) + 1
            if current["turns_played"] > 4:
                session["active_playlet"] = None
        else:
            session["active_playlet"] = {
                "id": pl["id"],
                "title": pl["title"],
                "setup_brief": pl["setup_brief"],
                "target_ids": pl["target_ids"],
                "turns_played": 1,
            }

    # 6. Build clean controllable UI Cards (max 3)
    ui_cards: list[dict] = []
    seen_cards: set[str] = set()
    requested_cards = meta.get("cards") if isinstance(meta.get("cards"), list) else []
    for cid in requested_cards:
        cid_str = str(cid).strip()
        if cid_str in seen_cards:
            continue
        card = curriculum.card_for(cid_str)
        if not card:
            dyn = next((d for d in personal_lexicon if d["id"] == cid_str or d["zh"] == cid_str), None)
            if dyn:
                card = {
                    "id": dyn["id"],
                    "kind": "personal_vocab",
                    "zh": dyn["zh"],
                    "pinyin": dyn["pinyin"],
                    "en": dyn["en"],
                    "emoji": dyn["emoji"],
                    "hook": dyn["hook"],
                }
        if card:
            seen_cards.add(card["id"])
            ui_cards.append(card)
        if len(ui_cards) >= 3:
            break

    # Auto-surface card if a brand-new target was exposed in speech and no card was explicitly listed
    if not ui_cards:
        for obs in applied_obs:
            if obs.get("effective_outcome") == "exposed":
                card = curriculum.card_for(obs["target_id"])
                if card and card["id"] not in seen_cards:
                    seen_cards.add(card["id"])
                    ui_cards.append(card)
                if len(ui_cards) >= 2:
                    break
        if isinstance(new_dyn, dict) and personal_lexicon and len(ui_cards) < 3:
            last_dyn = personal_lexicon[-1]
            if last_dyn["session_added"] == session_num and last_dyn["zh"] in reply_text and last_dyn["id"] not in seen_cards:
                ui_cards.append({
                    "id": last_dyn["id"],
                    "kind": "personal_vocab",
                    "zh": last_dyn["zh"],
                    "pinyin": last_dyn["pinyin"],
                    "en": last_dyn["en"],
                    "emoji": last_dyn["emoji"],
                    "hook": last_dyn["hook"],
                })

    return ui_cards, applied_obs


def reconcile_delivery(curriculum: Curriculum, profile_state: dict, reply_id: str, reply_obj: dict) -> None:
    """Roll back tentative first-exposure if audio playback was interrupted before target segment played."""
    ledger: dict = profile_state.get("mastery_ledger", {})
    overall = reply_obj.get("delivery")
    seg_map = {s["index"]: s["delivery"] for s in reply_obj.get("segments", [])}

    for tid, entry in ledger.items():
        if entry.get("pending_exposure_reply") != reply_id:
            continue
        req_segs = entry.get("pending_exposure_segments") or []
        if req_segs:
            heard = all(seg_map.get(idx) == "complete" for idx in req_segs)
            aborted = any(seg_map.get(idx) in {"interrupted", "failed"} for idx in req_segs) or (
                overall in {"interrupted", "failed"} and not heard
            )
        else:
            heard = overall == "complete"
            aborted = overall in {"interrupted", "failed"}

        if heard:
            entry["pending_exposure_reply"] = None
            entry["pending_exposure_segments"] = []
            entry["pre_exposure_stage"] = None
        elif aborted:
            if entry["stage"] == "exposed" and entry.get("uses", 0) == 0:
                entry["stage"] = entry.get("pre_exposure_stage") or "unseen"
                entry["exposures"] = max(0, entry.get("exposures", 1) - 1)
                intro = profile_state.get("active_session", {}).get("new_targets_introduced", [])
                if tid in intro:
                    intro.remove(tid)
            entry["pending_exposure_reply"] = None
            entry["pending_exposure_segments"] = []
            entry["modeled_in_reply"] = None
