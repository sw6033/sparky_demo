"""Streaming companion brain (OpenAI gpt-4.1 SSE + deterministic offline fallback)."""
from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from pathlib import Path
from typing import Iterator
from urllib.error import HTTPError

from . import connections, diagnostics, segments

PROMPT_DIR = Path(__file__).resolve().parent / "prompts"
SYSTEM_PROMPT = (PROMPT_DIR / "companion_system.md").read_text(encoding="utf-8").strip()
OPENING_PROMPT = (PROMPT_DIR / "session_opening.md").read_text(encoding="utf-8").strip()
SUMMARY_PROMPT = (PROMPT_DIR / "session_summary.md").read_text(encoding="utf-8").strip()

META_DELIMITER = "---META---"
DEFAULT_MODEL = "gpt-4.1"
DEFAULT_URL = "https://api.openai.com/v1/chat/completions"


def model_config() -> tuple[str, str, str]:
    key = os.getenv("SPARKY_MODEL_KEY") or os.getenv("OPENAI_API_KEY") or ""
    url = os.getenv("SPARKY_MODEL_URL") or (DEFAULT_URL if key else "")
    model = os.getenv("SPARKY_MODEL") or DEFAULT_MODEL
    return url, model, key


def configured() -> bool:
    url, model, _ = model_config()
    return bool(url and model)


def _clean_spoken(text: str) -> str:
    """Strip accidental markdown or parenthetical pinyin so TTS speaks naturally."""
    cleaned = text.replace("**", "").replace("*", "")
    # Remove parenthetical Latin pinyin annotations if model slipped any in
    cleaned = re.sub(r"\s*\([a-zA-Zāáǎàēéěèīíǐìōóǒòūúǔùǖǘǚǜ\s,'-]{2,35}\)", "", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _parse_meta_block(raw_meta: str) -> dict:
    raw = raw_meta.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.S).strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except ValueError:
        match = re.search(r"\{.*\}", raw, flags=re.S)
        if match:
            try:
                parsed = json.loads(match.group(0))
                return parsed if isinstance(parsed, dict) else {}
            except ValueError:
                pass
    return {}


def parse_two_part_output(full_text: str) -> tuple[str, dict]:
    if META_DELIMITER in full_text:
        spoken_part, meta_part = full_text.split(META_DELIMITER, 1)
        return _clean_spoken(spoken_part), _parse_meta_block(meta_part)
    # Graceful fallback if model emitted pure JSON or plain text
    stripped = full_text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        obj = _parse_meta_block(stripped)
        if isinstance(obj.get("reply"), str):
            return _clean_spoken(obj["reply"]), obj
    return _clean_spoken(full_text), {}


def stream_turn(user_text: str, turn_ctx: dict) -> Iterator[tuple]:
    """Yield ('sentence', text) as soon as each spoken sentence finishes, then ('done', full_reply, meta, stats)."""
    started = time.monotonic()
    url, model, key = model_config()
    if not url:
        reply, meta = offline_turn(user_text, turn_ctx)
        for s in segments.split_sentences(reply):
            yield ("sentence", s)
        yield ("done", reply, meta, {"source": "offline_fallback", "ttft_ms": 0.0, "brain_ms": diagnostics.elapsed(started)})
        return

    user_payload = {
        "child_utterance": user_text,
        "memory": turn_ctx["memory"],
        "recent_turns": turn_ctx["recent_turns"],
        "previous_reply_delivery": turn_ctx["last_reply_delivery"].get("delivery", "complete"),
        "session_focus": turn_ctx["focus"],
    }
    body = {
        "model": model,
        "stream": True,
        "temperature": 0.45,
        "max_tokens": 550,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ],
    }
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    if key:
        headers["Authorization"] = f"Bearer {key}"

    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
    raw_accum = ""
    emitted_count = 0
    ttft_ms: float | None = None
    first_sentence_ms: float | None = None

    try:
        with connections.open_response(req, timeout=25.0) as resp:
            while True:
                raw_line = resp.readline()
                if not raw_line:
                    resp.close()
                    break
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line or not line.startswith("data:"):
                    continue
                payload_str = line[5:].strip()
                if payload_str == "[DONE]":
                    resp.read()
                    resp.close()
                    break
                chunk = json.loads(payload_str)
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta", {}).get("content")
                if not delta:
                    continue
                if ttft_ms is None:
                    ttft_ms = diagnostics.elapsed(started)
                raw_accum += delta

                # Stream out complete sentences from spoken section while holding lookahead for ---META---
                if META_DELIMITER in raw_accum:
                    spoken_so_far = raw_accum.split(META_DELIMITER, 1)[0]
                    sents = segments.split_sentences(_clean_spoken(spoken_so_far))
                    while emitted_count < len(sents):
                        if first_sentence_ms is None:
                            first_sentence_ms = diagnostics.elapsed(started)
                        yield ("sentence", sents[emitted_count])
                        emitted_count += 1
                elif len(raw_accum) > 16:
                    safe_spoken = raw_accum[:-12]
                    sents = segments.split_sentences(_clean_spoken(safe_spoken))
                    # Emit all but the last partial sentence!
                    while emitted_count + 1 < len(sents):
                        if first_sentence_ms is None:
                            first_sentence_ms = diagnostics.elapsed(started)
                        yield ("sentence", sents[emitted_count])
                        emitted_count += 1

        spoken_reply, meta = parse_two_part_output(raw_accum)
        if not spoken_reply:
            raise ValueError("Empty spoken reply from model")
        final_sents = segments.split_sentences(spoken_reply)
        while emitted_count < len(final_sents):
            if first_sentence_ms is None:
                first_sentence_ms = diagnostics.elapsed(started)
            yield ("sentence", final_sents[emitted_count])
            emitted_count += 1

        yield (
            "done",
            spoken_reply,
            meta,
            {
                "source": "openai_stream",
                "model": model,
                "ttft_ms": ttft_ms or 0.0,
                "first_sentence_ms": first_sentence_ms or diagnostics.elapsed(started),
                "brain_ms": diagnostics.elapsed(started),
            },
        )
    except (HTTPError, OSError, TimeoutError, ValueError, KeyError) as exc:
        diagnostics.note("brain_error", error=type(exc).__name__)
        if emitted_count == 0:
            reply, meta = offline_turn(user_text, turn_ctx)
            for s in segments.split_sentences(reply):
                yield ("sentence", s)
            yield (
                "done",
                reply,
                meta,
                {"source": "offline_fallback_after_error", "error": type(exc).__name__, "brain_ms": diagnostics.elapsed(started)},
            )
        else:
            spoken_reply, meta = parse_two_part_output(raw_accum)
            yield (
                "done",
                spoken_reply or "Let's keep chatting!",
                meta,
                {"source": "partial_stream", "error": type(exc).__name__, "brain_ms": diagnostics.elapsed(started)},
            )


def request_json_sync(system_prompt: str, user_obj: dict, max_tokens: int = 350) -> dict | None:
    url, model, key = model_config()
    if not url:
        return None
    body = {
        "model": model,
        "temperature": 0.35,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(user_obj, ensure_ascii=False)},
        ],
    }
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
    try:
        with connections.urlopen(req, timeout=18.0) as resp:
            data = json.loads(resp.read())
        content = data["choices"][0]["message"]["content"]
        return _parse_meta_block(content)
    except Exception:
        return None


def generate_session_opening(mem_snapshot: dict, next_focus: dict) -> tuple[str, list[str]]:
    res = request_json_sync(OPENING_PROMPT, {"memory": mem_snapshot, "next_focus": next_focus}, max_tokens=220)
    if res and isinstance(res.get("opening"), str) and res["opening"].strip():
        cards = [str(c) for c in res.get("cards", []) if isinstance(c, str)][:2]
        return _clean_spoken(res["opening"]), cards
    return "", []


def summarize_session(session_num: int, turns: list[dict], mem: dict) -> dict | None:
    compact_turns = [{"child": t["user_text"], "sparky": t["reply_text"]} for t in turns[-20:]]
    res = request_json_sync(
        SUMMARY_PROMPT,
        {"session_num": session_num, "turns": compact_turns, "existing_facts": [f["text"] for f in mem.get("facts", [])]},
        max_tokens=300,
    )
    if not res or not isinstance(res.get("summary"), str):
        return None
    return {
        "session_num": session_num,
        "turn_count": len(turns),
        "summary": res["summary"].strip()[:220],
        "highlights": [str(x)[:100] for x in res.get("highlights", []) if isinstance(x, str)][:3],
        "open_threads": [str(x)[:120] for x in res.get("open_threads", []) if isinstance(x, str)][:2],
        "practiced_zh": [str(x)[:24] for x in res.get("practiced_zh", []) if isinstance(x, str)][:8],
    }


def offline_turn(user_text: str, turn_ctx: dict) -> tuple[str, dict]:
    """Curriculum-aware deterministic fallback so local demos & tests run cleanly without API keys."""
    text = user_text.strip()
    lower = text.casefold()
    focus = turn_ctx["focus"]
    targets = focus.get("primary_targets", [])
    snowball = focus.get("snowball_familiar_bank", [])

    meta: dict = {
        "move": "companionship_only",
        "cards": [],
        "observations": [],
        "child_name": None,
        "memory_notes": [],
        "new_personal_vocab": None,
        "playlet_state": None,
        "language_mode": None,
    }

    # 1. Explicit language switch
    if re.search(r"\b(speak|use|only)\s+english\b", lower) or "说英语" in text or "说英文" in text:
        meta["language_mode"] = "english"
        return "Got it! Let's stick to English whenever you want.", meta
    if re.search(r"\b(both languages|bilingual)\b", lower) or "双语" in text:
        meta["language_mode"] = "bilingual"
        return "Awesome! We'll chat in English and mix in fun Chinese together.", meta

    # 2. Extract child name or simple personal interest notes
    name_match = re.search(r"(?:my name is|i am|i'm|call me)\s+([A-Z][a-z]{1,15})\b", text) or re.search(r"我是([A-Za-z\u4e00-\u9fff]{1,8})", text)
    if name_match:
        cand = name_match.group(1)
        if cand.lower() not in {"good", "great", "happy", "sad", "hungry", "ready", "back", "here"}:
            meta["child_name"] = cand

    like_match = re.search(r"\bi (?:really )?(?:love|like|built|have|saw)\s+([^.!?]{2,45})", lower)
    if like_match:
        meta["memory_notes"].append(f"Shared: {text[:75]}")

    # 3. Decline detection
    if any(p in lower for p in ("no chinese", "don't want to practice", "stop the game", "no more game")) or any(
        p in text for p in ("不想学", "不想练", "不玩了")
    ):
        meta["move"] = "cooldown"
        meta["observations"].append({"target_id": "general", "outcome": "declined"})
        return "Totally fine! Let's just hang out and chat. What would you like to talk about?", meta

    # 4. Detect if child used any Chinese from primary_targets or snowball_bank!
    all_known = [*targets, *snowball]
    matched_zh = None
    for t in all_known:
        zh_core = re.sub(r"\{[a-zA-Z0-9_]+\}", "", t["zh"]).replace("？", "").replace("，", "").strip()
        if zh_core and zh_core in text:
            matched_zh = t
            st = t.get("current_stage") or t.get("stage", "exposed")
            outcome = "spontaneous_use" if st in {"scaffolded", "practiced", "mastered"} else "scaffolded_use"
            meta["observations"].append({"target_id": t["id"], "outcome": outcome})
            meta["cards"].append(t["id"])

    if focus["rhythm_mode"] in {"warmup_companionship", "cooldown_companionship", "english_only_requested"} and not matched_zh:
        greet = "你好! " if snowball and any(s["zh"] == "你好" for s in snowball) and focus["turn_index"] == 1 else ""
        if meta["child_name"]:
            return f"{greet}Awesome to meet you, {meta['child_name']}! What cool stuff did you do today?", meta
        return f"{greet}That sounds super fun! Tell me more about it!", meta

    if matched_zh:
        # Advance to next target or celebrate
        next_t = next((t for t in targets if t["id"] != matched_zh["id"]), None)
        if next_t:
            meta["move"] = "recast_expand"
            meta["cards"] = list(dict.fromkeys([matched_zh["id"], next_t["id"]]))[:3]
            if next_t["current_stage"] == "unseen":
                hook = next_t.get("hook") or next_t.get("en", "")
                return f"Nice one using {matched_zh['zh']}! Here's another fun one: {next_t['zh']} means {next_t['en']} — {hook}", meta
            return f"You nailed {matched_zh['zh']}! Want to try combining it with {next_t['zh']}?", meta
        meta["move"] = "recast_expand"
        return f"Woohoo, you used {matched_zh['zh']} like a natural! What else is on your mind?", meta

    # 5. Pedagogical move on top priority target
    if targets:
        top = targets[0]
        vocabs = [t for t in targets if t["kind"] == "vocab"]
        st = top["current_stage"]
        if st == "unseen":
            target_to_expose = vocabs[0] if vocabs and vocabs[0]["current_stage"] == "unseen" else top
            meta["move"] = "expose_in_context"
            meta["cards"] = [target_to_expose["id"]]
            if target_to_expose["kind"] == "vocab":
                return (
                    f"Speaking of fun stuff — in Chinese, {target_to_expose['en']} is {target_to_expose['zh']}! "
                    f"{target_to_expose.get('hook', '')}"
                ).strip(), meta
            expose_line = target_to_expose.get("scaffolds", {}).get("expose") or f"In Chinese we can say {target_to_expose['zh']}!"
            return expose_line, meta
        if st == "exposed":
            if len(vocabs) >= 2:
                meta["move"] = "either_or_choice"
                meta["cards"] = [vocabs[0]["id"], vocabs[1]["id"]]
                return f"Quick buddy pick in Chinese — {vocabs[0]['zh']} 还是 {vocabs[1]['zh']}?", meta
            meta["move"] = "cloze_starter"
            meta["cards"] = [top["id"]]
            cloze = (top.get("scaffolds") or {}).get("cloze") or f"Want to try saying {top['zh']}?"
            return cloze, meta
        meta["move"] = "situational_invite"
        meta["cards"] = [top["id"]]
        invite = (top.get("scaffolds") or {}).get("invite") or f"How would you use {top['en']} in Chinese right now?"
        return invite, meta

    return "That is super cool! What should we chat about or play next?", meta
