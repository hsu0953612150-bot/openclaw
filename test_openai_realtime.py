import unittest
from unittest.mock import patch

from openai_fallback import OpenAIFallbackUnavailable, create_realtime_client_secret


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class OpenAIRealtimeSecretTests(unittest.TestCase):
    def test_requires_server_api_key(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(OpenAIFallbackUnavailable):
                create_realtime_client_secret(api_key="")

    @patch("openai_fallback.urllib.request.urlopen")
    def test_creates_short_lived_audio_secret(self, urlopen):
        urlopen.return_value = FakeResponse({"value": "ek_test", "session": {"model": "gpt-realtime-mini"}})
        secret, model = create_realtime_client_secret(
            api_key="server-secret",
            instructions="繁體中文助理",
            model="gpt-realtime-mini",
        )
        request = urlopen.call_args.args[0]
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(secret, "ek_test")
        self.assertEqual(model, "gpt-realtime-mini")
        self.assertEqual(body["expires_after"]["seconds"], 600)
        self.assertEqual(body["session"]["audio"]["input"]["format"]["rate"], 24000)
        self.assertEqual(body["session"]["output_modalities"], ["audio"])
        self.assertEqual(request.get_header("Authorization"), "Bearer server-secret")


if __name__ == "__main__":
    unittest.main()

