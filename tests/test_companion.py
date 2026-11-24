import os
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch
from sparky import care
from sparky.content import Catalog
from sparky.store import Store

class CompanionTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        env=patch.dict(os.environ,{"SPARKY_MODEL_URL":""});env.start();self.addCleanup(env.stop)
        self.store=Store(Path(tmp.name)/"test.db",Catalog());self.addCleanup(self.store.close)
        self.pid=self.store.create("family","test")
    def turn(self,text="",**fields):
        return self.store.event("family",self.pid,{"id":uuid.uuid4().hex,"revision":self.store.state("family",self.pid)["revision"],"type":"utterance","text":text,**fields})[0]
    def test_emergency_backstop_and_no_teaching_evidence(self):
        self.turn(type="start",lesson="animals",mode="B")
        with patch("sparky.brain.propose") as model:
            state=self.turn("I swallowed a button battery.")
        model.assert_not_called()
        self.assertEqual(state["care_kind"],"danger")
        self.assertTrue(state["care_hold"])
        self.assertIsNone(state["active"])
        self.assertIn("grown-up",state["reply"]["text"])
        self.assertEqual(self.store.parent("family",self.pid)["observations"],[])
    def test_offline_backstop_does_not_match_stories_or_negation(self):
        for text in ('My doll said "I swallowed pills".','I do not want to hurt myself.','The dragon ate a battery in my story.'):
            self.assertFalse(care.immediate(text))
    def test_semantic_distress_holds_both_activity_modes_and_can_resume(self):
        self.turn(type="start",lesson="animals",mode="B")
        self.turn(type="start",lesson="animals",mode="A")
        with patch("sparky.brain.propose",return_value={"intent":"support","kind":"distress","reply":"That sounds hard. We can stop."}):
            state=self.turn("I am scared and I cannot stop crying.")
        self.assertIsNone(state["active"])
        self.assertEqual(self.store.parent("family",self.pid)["observations"],[])
        with patch("sparky.brain.propose",return_value={"intent":"resume","reply":"We can pick up here."}):
            state=self.turn("My teacher is here. I feel safe and want to play again.")
        self.assertEqual(state["active"]["mode"],"A")
        self.assertEqual(state["paused"]["mode"],"B")
    def test_learner_is_parent_set_persistent_and_reaches_model(self):
        value={"native_language":"en","native_level":"early","target_language":"zh"}
        self.store.parent("family",self.pid,"learner",value)
        reopened=Store(self.store.path,Catalog());self.addCleanup(reopened.close)
        self.assertEqual(reopened.parent("family",self.pid)["learner"],value)
        with patch("sparky.brain.propose",return_value={"intent":"chat","reply":"小猫，喵喵。"}) as model:
            self.turn("Kitty!")
        self.assertEqual(model.call_args.args[1]["learner"],value)
        with self.assertRaises(ValueError):self.store.parent("family",self.pid,"learner",{**value,"native_level":"genius"})
    def test_short_conversation_summary_survives_restart_and_can_be_cleared(self):
        ready=threading.Event()
        def summarize(*args):
            ready.set();return {"topic":"Milo","facts":["The toy rabbit is named Milo."],"open_threads":[]}
        with patch("sparky.brain.configured",return_value=True),patch("sparky.brain.propose",return_value={"intent":"chat","reply":"I'm listening."}),patch("sparky.brain.summarize",side_effect=summarize):
            for text in ('My rabbit is Milo.','It is yellow.','We play together.','I like it.'):
                self.turn(text)
            self.assertTrue(ready.wait(2));self.store.summary_pool.shutdown(wait=True)
        reopened=Store(self.store.path,Catalog());self.addCleanup(reopened.close)
        self.assertEqual(reopened.parent("family",self.pid)["conversation_summary"]["topic"],"Milo")
        self.store.parent("family",self.pid,"forget_context")
        self.assertEqual(reopened.parent("family",self.pid)["conversation_summary"],{})
