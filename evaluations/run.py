"""Replay synthetic Sparky evals, check pedagogical/voice invariants, optionally run LLM rubric judge, and diff baselines."""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from statistics import mean
import tempfile
import urllib.request

from sparky import brain, connections, segments
from sparky.curriculum import Curriculum
from sparky.store import Store


CASES_FILE = Path(__file__).with_name("cases.json")
MISSING = object()

PINYIN_PAREN_RE = re.compile(r"\([a-zA-Zāáǎàēéěèīíǐìōóǒòūúǔùǖǘǚǜ\s,'-]{2,35}\)")
EMOJI_RE = re.compile(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]")
MARKDOWN_HEADING_RE = re.compile(r"(?m)^#{1,6}\s")
COLD_QUIZ_RE = re.compile(
    r"\b(?:how do (?:you|we|i) say|what is|can you translate)\b[^.?!]{1,40}\bin (?:chinese|mandarin)\s*\?",
    re.IGNORECASE,
)
CJK_RE = re.compile(r"[\u3400-\u9fff]")

JUDGE_SYSTEM_PROMPT = """You are a strict QA evaluator for Sparky, a voice-first English/Mandarin companion for 5-8 year old children.
Evaluate the conversation trace against each rubric criterion in `criteria`.
Return ONLY JSON:
{"reviews": [{"criterion": "...", "passed": true, "reason": "short concrete explanation"}]}"""


def resolve(snapshot: dict, path: str):
    value = snapshot
    for key in path.split("."):
        if isinstance(value, dict):
            value = value.get(key, MISSING)
        elif isinstance(value, list) and key.isdecimal() and int(key) < len(value):
            value = value[int(key)]
        else:
            return MISSING
    return value


def matches(actual, operator: str, expected) -> bool:
    if actual is MISSING:
        return False
    if operator == "eq":
        return actual == expected
    if operator == "in":
        return actual in expected
    if operator == "contains":
        return expected in actual
    if operator == "any_text":
        return isinstance(actual, str) and any(text.casefold() in actual.casefold() for text in expected)
    if operator == "none_text":
        return isinstance(actual, str) and all(text.casefold() not in actual.casefold() for text in expected)
    raise ValueError(f"Unknown check operator: {operator}")


def compute_turn_invariants(reply_text: str, meta: dict, focus: dict, ui_cards: list[dict]) -> dict:
    """Deterministic protocol, TTS-cleanliness, and pedagogical boundary checks."""
    sents = segments.split_sentences(reply_text)
    has_pinyin = bool(PINYIN_PAREN_RE.search(reply_text))
    has_markdown = bool(
        brain.META_DELIMITER in reply_text
        or "**" in reply_text
        or "```" in reply_text
        or MARKDOWN_HEADING_RE.search(reply_text)
    )
    has_emoji = bool(EMOJI_RE.search(reply_text))
    voice_clean = bool(reply_text.strip()) and not has_pinyin and not has_markdown and not has_emoji and 1 <= len(sents) <= 4

    move = str(meta.get("move") or "companionship_only")
    rhythm_mode = focus.get("rhythm_mode")
    primary_targets = focus.get("primary_targets") or []
    passive_moves = {"companionship_only", "cooldown", "snowball_code_switch"}

    if rhythm_mode in {"warmup_companionship", "cooldown_companionship", "english_only_requested"} and not primary_targets:
        rhythm_respected = move in passive_moves
    else:
        allowed_union = set(passive_moves)
        for target in primary_targets:
            allowed_union.update(target.get("allowed_moves") or [])
        if focus.get("playlet"):
            allowed_union.add("playlet_turn")
        rhythm_respected = move in allowed_union

    all_unseen = bool(primary_targets) and all(t.get("current_stage") == "unseen" for t in primary_targets)
    asked_cold_quiz = bool(COLD_QUIZ_RE.search(reply_text)) and (all_unseen or not CJK_RE.search(reply_text))
    no_cold_quiz = not asked_cold_quiz

    valid_cards = len(ui_cards) <= 3 and all(isinstance(c.get("id"), str) and bool(c.get("zh")) for c in ui_cards)
    return {
        "voice_clean": voice_clean,
        "rhythm_respected": rhythm_respected,
        "no_cold_quiz": no_cold_quiz,
        "valid_cards": valid_cards,
        "sentence_count": len(sents),
    }


def judge_case_reviews(case: dict, steps: list[dict]) -> list[dict]:
    """Evaluate human rubric items automatically with an LLM judge when --judge is enabled."""
    criteria = case.get("review") or []
    if not criteria:
        return []
    url, default_model, key = brain.model_config()
    if not url:
        return [{"criterion": c, "passed": False, "reason": "Judge model URL not configured"} for c in criteria]
    judge_model = os.getenv("SPARKY_JUDGE_MODEL") or default_model

    compact_steps = []
    for idx, st in enumerate(steps, 1):
        entry = {"step": idx, "op": st.get("op")}
        if "user" in st:
            entry["user"] = st["user"]
            entry["sparky_reply"] = st.get("reply")
            entry["move"] = (st.get("meta") or {}).get("move")
            entry["rhythm_mode"] = (st.get("focus") or {}).get("rhythm_mode")
        elif st.get("op") == "next_session":
            entry["opening_reply"] = ((st.get("session") or {}).get("reply") or {}).get("text")
            entry["episodic_summaries"] = (st.get("memory") or {}).get("episodic_summaries")
            entry["facts"] = st.get("facts")
        compact_steps.append(entry)

    body = {
        "model": judge_model,
        "temperature": 0.0,
        "max_tokens": 450,
        "messages": [
            {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps({"case_id": case["id"], "trace": compact_steps, "criteria": criteria}, ensure_ascii=False)},
        ],
    }
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
    try:
        with connections.urlopen(req, timeout=25.0) as resp:
            data = json.loads(resp.read())
        parsed = brain._parse_meta_block(data["choices"][0]["message"]["content"])
        reviews = parsed.get("reviews")
        if not isinstance(reviews, list) or len(reviews) != len(criteria):
            return [{"criterion": c, "passed": False, "reason": "Malformed judge response"} for c in criteria]
        normalized = []
        for expected_c, item in zip(criteria, reviews):
            passed = bool(isinstance(item, dict) and item.get("passed") is True)
            reason = str(item.get("reason", "")).strip() if isinstance(item, dict) else "Invalid judge item"
            normalized.append({"criterion": expected_c, "passed": passed, "reason": reason})
        return normalized
    except Exception as exc:
        return [{"criterion": c, "passed": False, "reason": f"Judge error: {type(exc).__name__}"} for c in criteria]


def run_case(case: dict, store: Store, run_judge: bool = False) -> dict:
    profile = store.create_profile(case.get("name", "Leo"), preset=case.get("preset", "fresh"))["profile"]["id"]
    result = {"id": case["id"], "steps": [], "failures": []}
    for index, step in enumerate(case["steps"], 1):
        operation = step["op"]
        output = {}
        turn_ctx_focus = None
        if operation in {"turn", "fixture"}:
            turn_id = f"{case['id']}-{index}"
            context = store.prepare_turn_context(profile, turn_id, step["user"])
            turn_ctx_focus = context["focus"]
            if operation == "fixture":
                reply, meta, stats = step["reply"], step["meta"], {"source": "fixture"}
            elif case["mode"] == "offline":
                reply, meta = brain.offline_turn(step["user"], context)
                stats = {"source": "offline_fallback"}
            else:
                events = list(brain.stream_turn(step["user"], context))
                _, reply, meta, stats = events[-1]
                if stats.get("source") != "openai_stream":
                    result["failures"].append({"step": index, "check": "online_source", "actual": stats.get("source")})
            if operation == "turn" and case["mode"] == "online":
                if not all(isinstance(meta.get(key), field_type) for key, field_type in (
                    ("move", str), ("cards", list), ("observations", list),
                )):
                    result["failures"].append({"step": index, "check": "required_meta_fields"})
            store.commit_turn(profile, turn_id, step["user"], "text", reply, meta, context["focus"])
            output.update(user=step["user"], reply=reply, meta=meta, focus=context["focus"], stats=stats)
        elif operation == "delivery":
            reply_id = store.public_state(profile)["session"]["reply"]["id"]
            store.record_delivery(profile, reply_id, step["status"], step.get("segment"))
        elif operation == "next_session":
            if case["mode"] == "online":
                def summarize(*args):
                    episode = brain.summarize_session(*args)
                    output["model_summary"] = bool(episode)
                    return episode

                def open_session(*args):
                    opening, cards = brain.generate_session_opening(*args)
                    output["model_opening"] = bool(opening)
                    return opening, cards

                store.start_next_session(profile, summarize, open_session)
            else:
                store.start_next_session(profile)
        elif operation == "add_fact":
            store.mutate_memory(profile, operation, step["text"])
        elif operation == "remove_fact":
            facts = store.public_state(profile)["inspector"]["memory"]["facts"]
            fact_id = next(fact["id"] for fact in facts if fact["text"] == step["text"])
            store.mutate_memory(profile, operation, fact_id)
        else:
            raise ValueError(f"Unknown step operation: {operation}")

        state = store.public_state(profile)
        inspector = state["inspector"]
        progress = inspector["progress"]
        ui_cards = state["session"]["ui_cards"]
        if turn_ctx_focus is not None:
            output["invariants"] = compute_turn_invariants(output["reply"], output["meta"], turn_ctx_focus, ui_cards)
        output.update(
            op=operation,
            session=state["session"],
            language=state["language_mode"],
            memory=inspector["memory"],
            facts=[fact["text"] for fact in inspector["memory"]["facts"]],
            next_focus=inspector["focus"],
            stages={target["id"]: target["stage"] for unit in progress["units"] for target in unit["targets"]},
            active_unit=progress["active_unit_id"],
            personal_words=[entry["zh"] for entry in progress["personal_lexicon"]],
            card_words=[card["zh"] for card in ui_cards],
        )
        result["steps"].append(output)
        for path, operator, expected in step.get("checks", []):
            actual = resolve(output, path)
            if not matches(actual, operator, expected):
                result["failures"].append({
                    "step": index, "check": f"{path} {operator}", "expected": expected,
                    "actual": "<missing>" if actual is MISSING else actual,
                })

    result["review"] = case.get("review", [])
    if run_judge and result["review"]:
        judge_results = judge_case_reviews(case, result["steps"])
        result["judge_reviews"] = judge_results
        for jr in judge_results:
            if not jr["passed"]:
                result["failures"].append({
                    "step": "review",
                    "check": "judge_review",
                    "criterion": jr["criterion"],
                    "reason": jr["reason"],
                })
    result["passed"] = not result["failures"]
    return result


def _timing_summary(cases: list[dict]) -> dict:
    ttfts = []
    brains = []
    for c in cases:
        for st in c.get("steps", []):
            stats = st.get("stats") or {}
            if isinstance(stats.get("ttft_ms"), (int, float)) and stats.get("source") == "openai_stream":
                ttfts.append(float(stats["ttft_ms"]))
            if isinstance(stats.get("brain_ms"), (int, float)) and stats.get("source") == "openai_stream":
                brains.append(float(stats["brain_ms"]))
    return {
        "mean_ttft_ms": round(mean(ttfts), 1) if ttfts else None,
        "mean_brain_ms": round(mean(brains), 1) if brains else None,
    }


def compare_reports(baseline: dict, current: dict) -> dict:
    """Compare current eval report against a saved baseline report to gate regressions."""
    base_map = {c["id"]: bool(c.get("passed")) for c in baseline.get("cases", [])}
    curr_map = {c["id"]: bool(c.get("passed")) for c in current.get("cases", [])}
    shared_ids = sorted(set(base_map) & set(curr_map))

    regressions = [cid for cid in shared_ids if base_map[cid] and not curr_map[cid]]
    improvements = [cid for cid in shared_ids if not base_map[cid] and curr_map[cid]]
    added_cases = sorted(set(curr_map) - set(base_map))

    base_prompts = baseline.get("prompt_digests") or {}
    curr_prompts = current.get("prompt_digests") or {}
    changed_prompts = sorted(
        name for name in set(base_prompts) | set(curr_prompts) if base_prompts.get(name) != curr_prompts.get(name)
    )

    base_timing = _timing_summary(baseline.get("cases", []))
    curr_timing = _timing_summary(current.get("cases", []))
    return {
        "curriculum_changed": baseline.get("curriculum") != current.get("curriculum"),
        "changed_prompts": changed_prompts,
        "baseline_pass_rate": f"{baseline.get('passed', 0)}/{baseline.get('total', 0)}",
        "current_pass_rate": f"{current.get('passed', 0)}/{current.get('total', 0)}",
        "regressions": regressions,
        "improvements": improvements,
        "added_cases": added_cases,
        "timing": {"baseline": base_timing, "current": curr_timing},
    }


def build_report(mode: str, dataset: dict, curriculum: Curriculum, results: list[dict]) -> dict:
    passed = sum(result["passed"] for result in results)
    return {
        "version": dataset["version"],
        "mode": mode,
        "curriculum": curriculum.digest,
        "model": brain.model_config()[1] if mode == "online" else None,
        "prompt_digests": {
            name: sha256(prompt.encode()).hexdigest()
            for name, prompt in (
                ("companion", brain.SYSTEM_PROMPT),
                ("summary", brain.SUMMARY_PROMPT),
                ("opening", brain.OPENING_PROMPT),
            )
        },
        "timing": _timing_summary(results),
        "passed": passed,
        "total": len(results),
        "cases": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("offline", "online"), default="offline")
    parser.add_argument("--case", action="append", help="Run only this case ID; repeat to select multiple.")
    parser.add_argument("--judge", action="store_true", help="Score case review rubrics automatically with an LLM judge.")
    parser.add_argument("--save-baseline", type=Path, help="Write the full JSON report to this baseline path.")
    parser.add_argument("--compare", type=Path, help="Compare against a saved JSON baseline report; fail if any case regresses.")
    parser.add_argument("--json", action="store_true", help="Output a machine-readable report with synthetic turns.")
    args = parser.parse_args()

    dataset = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    cases = [case for case in dataset["cases"] if case["mode"] == args.mode]
    if args.case:
        unknown = set(args.case) - {case["id"] for case in cases}
        if unknown:
            parser.error(f"Unknown {args.mode} case(s): {', '.join(sorted(unknown))}")
        cases = [case for case in cases if case["id"] in args.case]
    if (args.mode == "online" or args.judge) and not brain.configured():
        parser.error("Online eval or --judge requires OPENAI_API_KEY or SPARKY_MODEL_URL. Offline is the default.")

    curriculum = Curriculum()
    results = []
    with tempfile.TemporaryDirectory(prefix="sparky-eval-") as temp_dir:
        for case in cases:
            store = Store(Path(temp_dir) / f"{case['id']}.sqlite3", curriculum)
            try:
                results.append(run_case(case, store, run_judge=args.judge))
            finally:
                store.close()

    report = build_report(args.mode, dataset, curriculum, results)
    if args.compare:
        baseline = json.loads(args.compare.read_text(encoding="utf-8"))
        report["comparison"] = compare_reports(baseline, report)
    if args.save_baseline:
        args.save_baseline.parent.mkdir(parents=True, exist_ok=True)
        args.save_baseline.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for result in results:
            print(f"{'PASS' if result['passed'] else 'FAIL'} {result['id']}")
            for failure in result["failures"]:
                print(f"  {json.dumps(failure, ensure_ascii=False)}")
        print(f"{report['passed']}/{report['total']} {args.mode} cases passed")
        if "comparison" in report:
            comp = report["comparison"]
            print(
                f"Baseline diff: {comp['baseline_pass_rate']} -> {comp['current_pass_rate']} | "
                f"regressions={comp['regressions']} | improvements={comp['improvements']} | "
                f"changed_prompts={comp['changed_prompts']}"
            )

    has_failures = report["passed"] != report["total"]
    has_regressions = bool(report.get("comparison", {}).get("regressions"))
    return int(has_failures or has_regressions)


if __name__ == "__main__":
    raise SystemExit(main())
