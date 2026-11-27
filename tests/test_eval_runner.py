import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from evaluations.run import CASES_FILE, compare_reports, compute_turn_invariants, run_case
from sparky.curriculum import Curriculum
from sparky.store import Store


class EvalRunnerTests(unittest.TestCase):
    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.curriculum = Curriculum()
        self.store = Store(Path(temp_dir.name) / "eval.sqlite3", self.curriculum)
        self.addCleanup(self.store.close)
        self.cases = json.loads(CASES_FILE.read_text(encoding="utf-8"))["cases"]

    def test_online_case_does_not_count_fallback_as_pass(self):
        case = next(case for case in self.cases if case["id"] == "online-support-before-teaching")
        reply = "Please get a trusted grown-up to help right now."
        meta = {"move": "companionship_only", "cards": [], "observations": []}
        with patch("evaluations.run.brain.stream_turn", return_value=iter([
            ("sentence", reply), ("done", reply, meta, {"source": "offline_fallback_after_error"}),
        ])):
            result = run_case(case, self.store)
        self.assertFalse(result["passed"])
        self.assertIn("online_source", [failure["check"] for failure in result["failures"]])

    def test_online_session_runs_summary_before_opening(self):
        case = next(case for case in self.cases if case["id"] == "online-grounded-next-session")
        episode = {"session_num": 2, "summary": "Maya made a blue helmet for Chomper.", "highlights": [], "open_threads": [], "practiced_zh": []}
        with patch("evaluations.run.brain.summarize_session", return_value=episode), patch(
            "evaluations.run.brain.generate_session_opening", return_value=("How is Chomper's helmet today?", []),
        ) as opening:
            result = run_case(case, self.store)
        self.assertTrue(result["passed"], result["failures"])
        self.assertEqual(opening.call_args.args[0]["past_sessions"][-1], episode)

    def test_turn_invariants_catch_pinyin_emoji_cold_quiz_and_rhythm_violation(self):
        warmup_focus = {"rhythm_mode": "warmup_companionship", "primary_targets": []}
        bad_inv = compute_turn_invariants(
            "Awesome! 🦖 Dinosaur is 恐龙 (kǒng lóng)!",
            {"move": "expose_in_context"},
            warmup_focus,
            [],
        )
        self.assertFalse(bad_inv["voice_clean"])
        self.assertFalse(bad_inv["rhythm_respected"])

        unseen_focus = {
            "rhythm_mode": "bridge_ready",
            "primary_targets": [{"id": "voc_dino", "current_stage": "unseen", "allowed_moves": ["expose_in_context"]}],
        }
        cold_quiz_inv = compute_turn_invariants(
            "A T-Rex! How do you say dinosaur in Chinese?",
            {"move": "expose_in_context"},
            unseen_focus,
            [],
        )
        self.assertTrue(cold_quiz_inv["voice_clean"])
        self.assertFalse(cold_quiz_inv["no_cold_quiz"])

    def test_judge_review_failure_marks_case_failed(self):
        case = next(case for case in self.cases if case["id"] == "online-support-before-teaching")
        reply = "Tell a trusted grown-up right away. By the way, arm is 手臂!"
        meta = {"move": "companionship_only", "cards": [], "observations": []}
        with patch("evaluations.run.brain.stream_turn", return_value=iter([
            ("sentence", reply), ("done", reply, meta, {"source": "openai_stream", "ttft_ms": 120.0, "brain_ms": 340.0}),
        ])), patch(
            "evaluations.run.judge_case_reviews",
            return_value=[{"criterion": case["review"][0], "passed": False, "reason": "Still tried to teach Chinese during injury."}],
        ):
            result = run_case(case, self.store, run_judge=True)
        self.assertFalse(result["passed"])
        self.assertEqual(result["failures"][0]["check"], "judge_review")

    def test_compare_reports_detects_regressions_improvements_and_prompt_changes(self):
        baseline = {
            "curriculum": "aaa111",
            "prompt_digests": {"companion": "old_hash", "summary": "same", "opening": "same"},
            "passed": 2,
            "total": 3,
            "cases": [
                {"id": "c1", "passed": True, "steps": []},
                {"id": "c2", "passed": True, "steps": []},
                {"id": "c3", "passed": False, "steps": []},
            ],
        }
        current = {
            "curriculum": "aaa111",
            "prompt_digests": {"companion": "new_hash", "summary": "same", "opening": "same"},
            "passed": 2,
            "total": 3,
            "cases": [
                {"id": "c1", "passed": True, "steps": []},
                {"id": "c2", "passed": False, "steps": []},
                {"id": "c3", "passed": True, "steps": []},
            ],
        }
        diff = compare_reports(baseline, current)
        self.assertEqual(diff["regressions"], ["c2"])
        self.assertEqual(diff["improvements"], ["c3"])
        self.assertEqual(diff["changed_prompts"], ["companion"])
        self.assertFalse(diff["curriculum_changed"])


if __name__ == "__main__":
    unittest.main()
