import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from sparky import mastery, segments
from sparky.curriculum import Curriculum
from sparky.store import Store


class CurriculumAndMasteryTests(unittest.TestCase):
    def setUp(self):
        self.curriculum = Curriculum()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "test.db", self.curriculum)

    def test_curriculum_loads_four_ordered_units_and_hot_reloads(self):
        summary = self.curriculum.summary()
        self.assertEqual(len(summary["units"]), 4)
        self.assertEqual([u["id"] for u in summary["units"]], ["u1_greetings", "u2_favorites", "u3_requests", "u4_descriptions"])

        # Copy curriculum to temp dir, mutate version/hook, verify hot reload updates digest
        custom_dir = Path(self.tmp.name) / "curr"
        custom_dir.mkdir()
        ped = json.loads((self.curriculum.directory / "pedagogy.json").read_text())
        syl = json.loads((self.curriculum.directory / "syllabus.json").read_text())
        (custom_dir / "pedagogy.json").write_text(json.dumps(ped))
        (custom_dir / "syllabus.json").write_text(json.dumps(syl))

        c2 = Curriculum(custom_dir)
        old_digest = c2.digest
        syl["version"] = 2
        (custom_dir / "syllabus.json").write_text(json.dumps(syl))
        c2.reload()
        self.assertNotEqual(old_digest, c2.digest)

    def test_curriculum_rejects_cyclic_prerequisites_and_bad_moves(self):
        custom_dir = Path(self.tmp.name) / "bad_curr"
        custom_dir.mkdir()
        ped = json.loads((self.curriculum.directory / "pedagogy.json").read_text())
        syl = json.loads((self.curriculum.directory / "syllabus.json").read_text())
        syl["units"][0]["prerequisites"] = ["u2_favorites"]
        (custom_dir / "pedagogy.json").write_text(json.dumps(ped))
        (custom_dir / "syllabus.json").write_text(json.dumps(syl))
        with self.assertRaises(ValueError):
            Curriculum(custom_dir)

        syl["units"][0]["prerequisites"] = []
        ped["mastery_ladder"]["unseen"]["allowed_moves"] = ["nonexistent_move"]
        (custom_dir / "pedagogy.json").write_text(json.dumps(ped))
        (custom_dir / "syllabus.json").write_text(json.dumps(syl))
        with self.assertRaises(ValueError):
            Curriculum(custom_dir)

    def test_warmup_rhythm_and_explicit_chinese_override(self):
        pub = self.store.create_profile("Leo", preset="fresh")
        pid = pub["profile"]["id"]

        # Turn 1 ordinary English sharing stays in warmup_companionship with no pushed targets
        ctx1 = self.store.prepare_turn_context(pid, "t1", "I built a tall tower today!")
        self.assertEqual(ctx1["focus"]["rhythm_mode"], "warmup_companionship")
        self.assertEqual(ctx1["focus"]["primary_targets"], [])

        # Explicit request to learn Chinese bypasses warmup wait immediately
        ctx_explicit = self.store.prepare_turn_context(pid, "t2", "How do I say hello in Chinese?")
        self.assertTrue(len(ctx_explicit["focus"]["primary_targets"]) >= 1)
        self.assertEqual(ctx_explicit["focus"]["active_unit"]["id"], "u1_greetings")

    def test_anti_echo_skip_invariant_and_interrupted_exposure_rollback(self):
        pub = self.store.create_profile("Leo", preset="fresh")
        pid = pub["profile"]["id"]

        # Turn 1: Sparky exposes 你好 in segment 1
        ctx1 = self.store.prepare_turn_context(pid, "t1", "Teach me Chinese!")
        reply1 = "Welcome buddy! In Chinese, hello is 你好."
        st1 = self.store.commit_turn(pid, "t1", "Teach me Chinese!", "text", reply1, {"move": "expose_in_context", "cards": ["voc_nihao"]}, ctx1["focus"])
        rid1 = st1["session"]["reply"]["id"]

        # Check tentative exposure
        u1_targets = {t["id"]: t["stage"] for t in st1["inspector"]["progress"]["units"][0]["targets"]}
        self.assertEqual(u1_targets["voc_nihao"], "exposed")

        # Simulate interrupted playback on segment 0 before segment 1 ("你好") was heard -> rolls back to unseen!
        self.store.record_delivery(pid, rid1, "interrupted", segment=0)
        after_interrupt = self.store.public_state(pid)
        u1_targets = {t["id"]: t["stage"] for t in after_interrupt["inspector"]["progress"]["units"][0]["targets"]}
        self.assertEqual(u1_targets["voc_nihao"], "unseen")

        # Turn 2: Expose again and complete playback
        ctx2 = self.store.prepare_turn_context(pid, "t2", "Say that again please!")
        st2 = self.store.commit_turn(pid, "t2", "Say that again please!", "text", "Hello in Chinese is 你好!", {"move": "expose_in_context"}, ctx2["focus"])
        self.store.record_delivery(pid, st2["session"]["reply"]["id"], "complete")

        # Turn 3: Child echoes "你好!" right after Sparky modeled it.
        # Even if LLM META over-generously claims "spontaneous_use", invariant caps it to "imitated" -> "scaffolded"!
        ctx3 = self.store.prepare_turn_context(pid, "t3", "你好!")
        st3 = self.store.commit_turn(
            pid,
            "t3",
            "你好!",
            "text",
            "Nice! You said it!",
            {"move": "recast_expand", "observations": [{"target_id": "voc_nihao", "outcome": "spontaneous_use"}]},
            ctx3["focus"],
        )
        u1_targets = {t["id"]: t["stage"] for t in st3["inspector"]["progress"]["units"][0]["targets"]}
        self.assertEqual(u1_targets["voc_nihao"], "scaffolded")

    def test_decline_triggers_cooldown_and_clears_playlet(self):
        pub = self.store.create_profile("Maya", preset="returning_dino")
        pid = pub["profile"]["id"]
        ctx1 = self.store.prepare_turn_context(pid, "t1", "Let's play a game!")
        st1 = self.store.commit_turn(
            pid,
            "t1",
            "Let's play a game!",
            "text",
            "Let's play Would You Rather!",
            {"move": "playlet_turn", "playlet_state": "play_would_you_rather"},
            ctx1["focus"],
        )
        self.assertIsNotNone(st1["session"]["active_playlet"])

        ctx2 = self.store.prepare_turn_context(pid, "t2", "No more Chinese game, I want to talk about recess.")
        st2 = self.store.commit_turn(
            pid,
            "t2",
            "No more Chinese game, I want to talk about recess.",
            "text",
            "Of course! Tell me all about recess!",
            {"move": "cooldown", "observations": [{"target_id": "general", "outcome": "declined"}]},
            ctx2["focus"],
        )
        self.assertIsNone(st2["session"]["active_playlet"])
        ctx3 = self.store.prepare_turn_context(pid, "t3", "We played tag.")
        self.assertEqual(ctx3["focus"]["rhythm_mode"], "cooldown_companionship")
        self.assertEqual(ctx3["focus"]["primary_targets"], [])

    def test_dynamic_personal_vocab_and_unit_unlock(self):
        pub = self.store.create_profile("Maya", preset="returning_dino")
        pid = pub["profile"]["id"]
        # Returning preset already completed Unit 1 -> active unit is u2_favorites!
        self.assertEqual(pub["inspector"]["progress"]["active_unit_id"], "u2_favorites")

        ctx = self.store.prepare_turn_context(pid, "t1", "I love hammerhead sharks!")
        st = self.store.commit_turn(
            pid,
            "t1",
            "I love hammerhead sharks!",
            "text",
            "Sharks are epic! Shark in Chinese is 鲨鱼 — 我喜欢鲨鱼!",
            {
                "move": "expose_in_context",
                "new_personal_vocab": {"zh": "鲨鱼", "pinyin": "shā yú", "en": "shark", "emoji": "🦈", "hook": "shā yú means sand-fish!"},
            },
            ctx["focus"],
        )
        lex = st["inspector"]["progress"]["personal_lexicon"]
        self.assertEqual(len(lex), 1)
        self.assertEqual(lex[0]["zh"], "鲨鱼")
        self.assertTrue(any(c["zh"] == "鲨鱼" for c in st["session"]["ui_cards"]))


if __name__ == "__main__":
    unittest.main()
