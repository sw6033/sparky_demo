import concurrent.futures
import json
import os
import shutil
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch
from sparky.content import Catalog, ROOT
from sparky.store import Conflict, Missing, Store

class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env=patch.dict(os.environ,{"SPARKY_MODEL_URL":""})
        env.start()
        self.addCleanup(env.stop)
        self.db=Path(self.tmp.name)/"test.sqlite3"
        self.catalog=Catalog()
        self.store=Store(self.db,self.catalog)
        self.pid=self.store.create("family","测试")
    def event(self,kind,**args):
        return dict(id=uuid.uuid4().hex,revision=self.snapshot()["revision"],type=kind,**args)
    def turn(self,kind,**kwargs):
        return self.store.event("family",self.pid,self.event(kind,**kwargs))[0]
    def snapshot(self):
        return self.store.state("family",self.pid)
    def observations(self):
        return self.store.parent("family",self.pid)["observations"]
    def start(self,mode="B",lesson="animals"):
        return self.turn("start",mode=mode,lesson=lesson)

    def test_all_lessons_use_same_runner(self):
        for lesson in self.catalog.lessons.values():
            if lesson["activities"]["B"]!="explore":
                continue
            with self.subTest(lesson=lesson["id"]):
                item=lesson["items"][-1]
                self.start(lesson=lesson["id"])
                self.turn("utterance",text=item["english"][0])
                self.assertEqual(self.snapshot()["active"]["step"],2)
                self.turn("utterance",text=item["forms"][0])
                self.assertIsNone(self.snapshot()["active"])
                self.assertEqual(self.snapshot()["recent"]["result"],"completed")
        kinds=[x["kind"] for x in self.observations()]
        self.assertEqual(kinds.count("independent"),3)
        self.assertIn("english_choice",kinds)

    def test_mixed_and_wrong_answer(self):
        self.start()
        self.turn("utterance",text="我选 dog")
        self.turn("utterance",text="这是小猫")
        self.assertEqual(self.snapshot()["active"]["step"],2)
        self.assertIn("meaning_with_frame",[r["kind"] for r in self.observations()])

    def test_hint_delivery_and_suspended_story(self):
        self.start()
        self.turn("select",item="dog")
        self.start("A")
        help_reply=self.turn("help")["reply"]["id"]
        self.store.delivery("family",self.pid,help_reply,"started","first")
        self.store.delivery("family",self.pid,help_reply,"interrupted","first")
        self.store.delivery("family",self.pid,help_reply,"complete","first")
        self.assertEqual(self.snapshot()["reply"]["delivery"],"interrupted")
        self.store.delivery("family",self.pid,help_reply,"started","second")
        self.store.delivery("family",self.pid,help_reply,"complete","second")
        self.turn("utterance",text="这是小狗")
        self.assertIsNone(self.snapshot()["active"])
        self.assertEqual(self.snapshot()["paused"]["step"],2)
        self.store=Store(self.db,Catalog())
        self.turn("resume")
        self.turn("utterance",text="这是一只狗")
        kinds=[x["kind"] for x in self.observations()]
        self.assertIn("imitation",kinds)
        self.assertIn("supported",kinds)
        self.assertNotIn("independent",kinds)

    def test_pause_resume_uncertainty_and_refusal(self):
        self.start()
        self.turn("utterance",text="这是小猫",confidence=0.1)
        self.turn("utterance",text="这是小猫",speaker="unknown")
        self.turn("silence")
        self.assertIn("停一停",self.snapshot()["reply"]["text"])
        self.turn("utterance",text="什么意思")
        self.turn("utterance",text="不想跟读")
        self.assertEqual(self.snapshot()["active"]["step"],1)
        self.turn("pause")
        self.turn("resume")
        self.assertEqual(self.snapshot()["active"]["step"],1)
        self.turn("finish")
        self.assertEqual(self.snapshot()["recent"]["result"],"ended")
        self.assertNotIn("independent",[r["kind"] for r in self.observations()])

    def test_one_input_one_transition_even_under_race(self):
        self.start()
        e=self.event("select",item="cat")
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            results=list(pool.map(lambda _:self.store.event("family",self.pid,e), range(2)))
        self.assertEqual(sorted(x[1] for x in results),[False,True])
        self.assertEqual(sum(x["n"] for x in self.observations()),1)
        with self.assertRaises(Conflict):
            self.store.event("family",self.pid,{**e,"type":"pause"})
        with self.assertRaises(Conflict):
            self.store.event("family",self.pid,{**e,"id":"other"})

    def test_content_release_and_new_lesson_without_code(self):
        folder=Path(self.tmp.name)/"curriculum"
        shutil.copytree(ROOT/"curriculum",folder)
        path=folder/"lessons"/"color.json"
        copy=json.loads(path.read_text())
        copy.update(id="paint",title="新画室",version=1)
        path=folder/"lessons"/"paint.json"
        path.write_text(json.dumps(copy))
        self.store=Store(self.db,Catalog(folder))
        self.start(lesson="paint")
        self.turn("pause")
        copy["version"]=2
        copy["prompts"]["opening"]="新版情境"
        path.write_text(json.dumps(copy))
        self.store=Store(self.db,Catalog(folder))
        self.turn("resume")
        self.assertNotIn("新版",self.snapshot()["active"]["question"])
        self.turn("finish")
        self.start(lesson="paint")
        self.assertIn("新版",self.snapshot()["active"]["question"])

    def test_bad_template_rejected_before_session(self):
        folder=Path(self.tmp.name)/"curriculum"
        shutil.copytree(ROOT/"curriculum",folder)
        path=folder/"templates"/"explore.json"
        version=json.loads(path.read_text())
        version["steps"][0]["goal"]="missing"
        path.write_text(json.dumps(version))
        with self.assertRaises(ValueError):
            Catalog(folder)

    def test_privacy_and_atomic_delete(self):
        self.start()
        self.turn("select",item="cat")
        m=self.store.parent("family",self.pid,"remember","喜欢画画")["memories"][0]
        with self.assertRaises(Missing):
            self.store.parent("someone_else",self.pid)
        self.assertFalse(self.store.parent("family",self.pid,"forget",m["id"])["memories"])
        self.store.parent("family",self.pid,"delete")
        with self.assertRaises(Missing):
            self.snapshot()
        with self.store.db() as db:
            for table in ("events","memories","observations"):
                self.assertFalse(db.execute("SELECT * FROM "+table).fetchall())



    def test_receptive_learning_does_not_require_speaking(self):
        self.start(lesson="body")
        self.turn("select",item="nose")
        self.assertEqual(self.snapshot()["active"]["step"],1)
        self.turn("select",item="hand")
        self.assertIn("鼻子",self.snapshot()["active"]["question"])
        self.turn("select",item="nose")
        self.assertIsNone(self.snapshot()["active"])
        counts={r["kind"]:r["n"] for r in self.observations()}
        self.assertEqual(counts,{"other_choice":1,"identified":2})

    def test_spoken_or_typed_lesson_requests(self):
        self.turn("utterance",text="我喜欢颜色")
        self.assertIsNone(self.snapshot()["active"])
        self.turn("utterance",text="我想玩颜色")
        self.assertEqual(self.snapshot()["active"]["lesson"],"color")
        self.turn("utterance",text="蓝色")
        self.assertEqual(self.snapshot()["active"]["step"],2)
        self.turn("pause")
        self.turn("utterance",text="继续")
        self.assertEqual(self.snapshot()["active"]["step"],2)

    def test_teach_in_conversation_without_advancing_an_activity(self):
        def say(text):
            return self.turn("utterance", text=text)["reply"]["text"]
        self.assertIn("小猫", say("I saw a cat!"))
        self.assertIn("睡着了", say("It's sleeping."))
        self.assertIn("蓝色", say("I like blue"))
        self.assertNotEqual(say("blue"), "蓝色，像晴天的天空。")
        self.assertIn("谢谢", say("Thank you!"))
        self.assertIsNone(self.snapshot()["active"])
        self.assertFalse(self.observations())
        self.assertIsNone(self.catalog.conversation_entry("I'm tired of catching balloons"))
        self.start(lesson="color")
        self.assertIn("兔子", say("I have a bunny"))
        self.assertEqual(self.snapshot()["active"]["step"],1)
        say("blue")
        self.assertEqual(self.snapshot()["active"]["step"],2)

    def test_activity_lifecycle_by_conversation(self):
        def talk(text):
            return self.turn("utterance",text=text)
        greeting=talk("你好，Sparky！")
        self.assertEqual(greeting["reply"]["text"],"你好呀！我在呢。")
        self.assertIsNone(greeting["active"])
        first=talk("有什么好玩的")
        self.assertIn("好吗",first["reply"]["text"])
        self.assertEqual(talk("好呀")["active"]["mode"],"B")
        self.assertEqual(talk("我想画画")["active"]["lesson"],"color")
        talk("蓝色")
        self.assertEqual(talk("练一句中文")["active"]["mode"],"A")
        talk("帮帮我")
        self.assertEqual(talk("继续故事")["active"]["step"],2)
        talk("休息一下")
        self.assertIsNotNone(talk("继续")["active"])
        self.assertIsNone(talk("不玩了")["active"])
        self.assertEqual(talk("找小猫")["active"]["lesson"],"animals")
