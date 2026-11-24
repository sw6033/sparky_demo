import io
import json
import os
import unittest
from unittest.mock import patch

from sparky import brain


class BrainTests(unittest.TestCase):
    def test_language_policy_and_preference_reach_model(self):
        result={"intent":"language","preference":"english"}
        response={"choices":[{"message":{"content":json.dumps(result)}}]}
        with patch.dict(os.environ,{"SPARKY_MODEL":"fixture","SPARKY_MODEL_URL":"https://example.invalid"}), \
             patch("sparky.brain.urllib.request.urlopen",return_value=io.BytesIO(json.dumps(response).encode())) as upstream:
            self.assertEqual(brain.propose("Let's use English",{"language_preference":"bilingual"}),result)
            payload=json.loads(upstream.call_args.args[0].data)
        self.assertIn("默认以简单自然的英语交流",payload["messages"][0]["content"])
        self.assertIn("当前明确的语言要求优先",payload["messages"][0]["content"])
        self.assertEqual(payload["messages"][0]["content"],brain.CORE_PROMPT+"\n\n"+brain.PROTOCOL_PROMPT)
        self.assertEqual(json.loads(payload["messages"][1]["content"])["context"]["language_preference"],"bilingual")

    def test_invalid_preferences_never_enter_state(self):
        for value in [None,"french",[],{"language":"english"}]:
            with self.subTest(preference=value):
                response={"choices":[{"message":{"content":json.dumps({"intent":"language","preference":value})}}]}
                with patch.dict(os.environ,{"SPARKY_MODEL":"fixture","SPARKY_MODEL_URL":"https://example.invalid"}), \
                     patch("sparky.brain.urllib.request.urlopen",return_value=io.BytesIO(json.dumps(response).encode())):
                    self.assertIsNone(brain.propose("hi",{}))
