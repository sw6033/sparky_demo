import io
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from sparky.live import connect


class NegotiationTests(unittest.TestCase):
    @patch("sparky.live.request.urlopen")
    def test_server_exchanges_offer_and_only_returns_answer(self, upstream):
        upstream.return_value = io.BytesIO(b'{"transport":{"sdp":"v=0 answer"},"session":{"id":"test"}}')
        self.assertEqual(connect("fixture", "v=0 offer"), (201, {"sdp":"v=0 answer"}))
        req = upstream.call_args.args[0]
        body = json.loads(req.data)
        self.assertEqual(req.full_url, "https://api.openai.com/v1/live/sessions")
        self.assertEqual(req.get_header("Authorization"), "Bearer fixture")
        self.assertEqual(body["transport"], {"type":"webrtc", "sdp":"v=0 offer"})
        self.assertFalse(body["session"]["store"])

    @patch("sparky.live.request.urlopen")
    def test_redacts_upstream_authentication_error(self, upstream):
        upstream.side_effect = HTTPError("", 401, "secret upstream message", {}, None)
        status, result = connect("fixture", "v=0 offer")
        self.assertEqual(status, 502)
        self.assertIn("401", result["error"])
        self.assertNotIn("secret", result["error"])
