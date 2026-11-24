import json
import os
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch
from sparky import brain, segments
from sparky.content import Catalog
from sparky.store import Store

class OptimizationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env=patch.dict(os.environ,{"SPARKY_MODEL_URL":"","SPARKY_DEBUG_TRANSCRIPTS":""})
        env.start();self.addCleanup(env.stop)
        self.store=Store(Path(self.tmp.name)/"test.db",Catalog())
        self.addCleanup(self.store.close)
        self.pid=self.store.create("family","test")
    def state(self):
        return self.store.state("family",self.pid)
    def turn(self,text=None,**kw):
        event={"id":uuid.uuid4().hex,"revision":self.state()["revision"],"type":"utterance","text":text,**kw}
        return self.store.event("family",self.pid,event)[0]

    def test_semantic_activity_tools_and_unified_reply_single_call(self):
        cases=[("Can we draw?",{"intent":"start","lesson":"color","mode":"B","reply":"Let's paint together! Red or blue?"}),
               ("Let's do animals instead",{"intent":"change_activity","lesson":"animals","mode":"B","reply":"Let's find a little animal! A cat or a dog?"}),
               ("Hold our place for a moment",{"intent":"pause","reply":"Sure, I'll save our place."}),
               ("Let's pick up where we left off",{"intent":"resume","reply":"Welcome back! A cat or a dog?"})]
        for text,result in cases:
            with patch("sparky.brain.propose",return_value=result) as model:
                state=self.turn(text)
                self.assertEqual(state["reply"]["text"],result["reply"])
                self.assertEqual(model.call_count,1)
                self.assertTrue(model.call_args.args[1]["available_actions"])
                if result["intent"] in {"start","change_activity","resume"}:
                    contract=next(a for a in model.call_args.args[1]["available_actions"]
                        if all(a.get(k)==v for k,v in result.items() if k!="reply"))
                    self.assertTrue(contract["capability"])
        self.assertEqual(state["active"]["lesson"],"animals")
        self.assertEqual(state["active"]["step"],1)

    def test_interest_is_chat_and_illegal_action_is_rejected(self):
        with patch("sparky.brain.propose",return_value={"intent":"chat","reply":"What do you enjoy drawing?"}) as model:
            self.assertIsNone(self.turn("I like drawing")["active"])
            context=model.call_args.args[1]
            chat=next(a for a in context["available_actions"] if a["intent"]=="chat")
            self.assertNotIn("draft_reply",chat)
            self.assertIn("reply_guidance",context)
        with patch("sparky.brain.propose",return_value={"intent":"start","lesson":"missing","mode":"B","reply":"Started!"}):
            state=self.turn("Start a dragon activity")
        self.assertIsNone(state["active"])
        self.assertNotEqual(state["reply"]["text"],"Started!")
        rows=self.store.debug("family",self.pid)["turns"]
        self.assertTrue(any(d.get("status")=="action_rejected" for r in rows for d in r.get("decision",[])))

    def test_required_example_is_repaired_and_segment_delivery_counts_only_heard_example(self):
        self.turn(type="start",lesson="animals",mode="A")
        answers=[{"intent":"help","reply":"You can say anything!"},
                 {"intent":"help","reply":'Try this: "这是小猫". Take your time.'}]
        with patch("sparky.brain.propose",side_effect=answers) as model:
            state=self.turn("Help me")
            self.assertEqual(model.call_count,2)
        reply=state["reply"]
        self.assertEqual(len(reply["segments"]),2)
        for phase in ("started","complete"):
            self.store.delivery("family",self.pid,reply["id"],phase,"a",0)
        self.store.delivery("family",self.pid,reply["id"],"started","b",1)
        self.store.delivery("family",self.pid,reply["id"],"interrupted","b",1)
        self.assertEqual(self.state()["reply"]["delivery"],"interrupted")
        self.turn("这是小猫")
        kinds=[r["kind"] for r in self.store.parent("family",self.pid)["observations"]]
        self.assertIn("imitation",kinds)

    def test_unheard_example_is_not_imitation(self):
        self.turn(type="start",lesson="animals",mode="A")
        with patch("sparky.brain.propose",return_value={"intent":"help","reply":'Take your time. You can say "这是小猫".'}):
            reply=self.turn("Help me")["reply"]
        for phase in ("started","complete"):
            self.store.delivery("family",self.pid,reply["id"],phase,"a",0)
        self.store.delivery("family",self.pid,reply["id"],"failed","b",1)
        self.turn("这是小猫")
        kinds=[r["kind"] for r in self.store.parent("family",self.pid)["observations"]]
        self.assertIn("supported",kinds)
        self.assertNotIn("imitation",kinds)

    def test_short_whole_audio_interruption_does_not_claim_heard_example(self):
        self.turn(type="start",lesson="animals",mode="A")
        with patch("sparky.brain.propose",return_value={"intent":"help","reply":'Try this: "这是小猫". Take your time.'}):
            reply=self.turn("Help me")["reply"]
        self.assertEqual(reply["speech_mode"],"whole")
        for phase in ("started","interrupted"):
            self.store.delivery("family",self.pid,reply["id"],phase,"whole")
        self.assertTrue(all(p["delivery"]=="interrupted" for p in self.state()["reply"]["segments"]))
        self.turn("这是小猫")
        kinds=[r["kind"] for r in self.store.parent("family",self.pid)["observations"]]
        self.assertNotIn("imitation",kinds)

    def test_short_whole_audio_completion_preserves_heard_example(self):
        self.turn(type="start",lesson="animals",mode="A")
        with patch("sparky.brain.propose",return_value={"intent":"help","reply":'Try this: "这是小猫". Take your time.'}):
            reply=self.turn("Help me")["reply"]
        for phase in ("started","complete"):
            self.store.delivery("family",self.pid,reply["id"],phase,"whole")
        self.turn("这是小猫")
        kinds=[r["kind"] for r in self.store.parent("family",self.pid)["observations"]]
        self.assertIn("imitation",kinds)

    def test_audio_policy_batches_short_replies_but_keeps_long_sentences(self):
        short='Let us draw a little animal together. Would you like to draw a cat or a dog?'
        self.assertEqual(segments.attach({"text":short})["speech_mode"],"whole")
        self.assertEqual(segments.attach({"text":"One. Two. Three."})["speech_mode"],"sentences")
        self.assertEqual(segments.attach({"text":"你好"*30+"。"})["speech_mode"],"sentences")

    def test_delivery_and_notes_are_in_next_model_context(self):
        self.turn(type="start",lesson="animals",mode="A")
        reply=self.turn("help")["reply"]
        self.store.delivery("family",self.pid,reply["id"],"started","play",0)
        self.store.delivery("family",self.pid,reply["id"],"interrupted","play",0)
        with patch("sparky.brain.propose",return_value={"intent":"chat","reply":"What's on your mind?"}) as model:
            self.turn("Wait, something else")
        context=model.call_args.args[1]
        self.assertEqual(context["delivery_context"]["delivery"],"interrupted")
        self.assertEqual(context["recent_turns"][-1]["delivery"],"interrupted")
        self.assertEqual(context["session_notes"][-1]["demonstration"],"这是小猫")

    def test_background_summary_survives_restart_and_does_not_block_turn(self):
        entered=threading.Event();release=threading.Event()
        def summary(*args):
            entered.set();release.wait(3)
            return {"topic":"A toy rabbit","facts":["The child has a toy rabbit."],"open_threads":[]}
        with patch("sparky.brain.configured",return_value=True), patch("sparky.brain.propose",return_value={"intent":"chat","reply":"Tell me more."}), patch("sparky.brain.summarize",side_effect=summary):
            try:
                for i in range(10):self.turn("My toy rabbit story "+str(i))
                self.assertTrue(entered.wait(1))
                self.assertEqual(self.state()["revision"],10)
                with patch("sparky.brain.propose",return_value={"intent":"chat","reply":"I'm still here."}) as model:
                    self.turn("Keep listening")
                    self.assertEqual(model.call_args.args[1]["recent_turns"][0]["user"],"My toy rabbit story 0")
            finally:
                release.set();self.store.summary_pool.shutdown(wait=True)
        self.assertEqual(self.state()["conversation_summary"]["topic"],"A toy rabbit")
        reopened=Store(self.store.path,Catalog());self.addCleanup(reopened.close)
        self.assertEqual(reopened.state("family",self.pid)["conversation_summary"]["facts"],["The child has a toy rabbit."])

    def test_forgetting_invalidates_pending_summary_and_traces(self):
        entered=threading.Event();release=threading.Event()
        def summary(*args):
            entered.set();release.wait(3)
            return {"topic":"forgotten","facts":[],"open_threads":[]}
        memory=self.store.parent("family",self.pid,"remember","a preference")["memories"][0]["id"]
        with patch("sparky.brain.configured",return_value=True), patch("sparky.brain.propose",return_value={"intent":"chat","reply":"Tell me more."}), patch("sparky.brain.summarize",side_effect=summary):
            try:
                for i in range(10):self.turn("story "+str(i))
                self.assertTrue(entered.wait(1))
                self.store.parent("family",self.pid,"forget",memory)
            finally:
                release.set();self.store.summary_pool.shutdown(wait=True)
        self.assertEqual(self.state()["conversation_summary"],{})
        self.assertEqual(self.store.debug("family",self.pid)["turns"],[])

    def test_trace_defaults_to_metadata_and_split_preserves_quoted_examples(self):
        self.turn("Private story about my pet")
        data=json.dumps(self.store.debug("family",self.pid)["turns"])
        self.assertNotIn("Private story",data)
        self.assertIn("unconfigured",data)
        self.assertEqual([p["text"] for p in segments.split('Say "你好。小猫！" gently. Then wait.')],['Say "你好。小猫！" gently.','Then wait.'])

    def test_model_failure_is_diagnosable_without_leaking_response(self):
        from urllib.error import HTTPError
        with patch.dict(os.environ,{"SPARKY_MODEL":"fixture","SPARKY_MODEL_URL":"https://example.invalid"}), patch("sparky.brain.urllib.request.urlopen",side_effect=HTTPError("",429,"private upstream message",{},None)):
            self.turn("Tell me about the moon")
        data=json.dumps(self.store.debug("family",self.pid)["turns"])
        self.assertIn("http_error",data)
        self.assertIn("429",data)
        self.assertNotIn("private upstream",data)
