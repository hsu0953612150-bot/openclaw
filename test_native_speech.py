import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import urllib.error

import native_speech as speech


class NativeSpeechTests(unittest.TestCase):
    def setUp(self):
        speech._primary_retry_after = 0
        speech._fallback_retry_after = 0

    def test_primary_success_never_calls_paid_fallback(self):
        fallback = Mock()
        self.assertEqual(speech.synthesize_native("你好", lambda _: (b"mp3", 1000), fallback), (b"mp3", 1000))
        fallback.assert_not_called()

    def test_primary_failure_uses_fallback_and_cooldown(self):
        primary = Mock(side_effect=RuntimeError("quota"))
        fallback = Mock(return_value=(b"mp3", 1500))
        for _ in range(2):
            self.assertEqual(speech.synthesize_native("你好", primary, fallback), (b"mp3", 1500))
        primary.assert_called_once()
        self.assertEqual(fallback.call_count, 2)

    def test_both_fail_without_repeated_calls_in_cooldown(self):
        primary = Mock(side_effect=RuntimeError("quota"))
        fallback = Mock(side_effect=RuntimeError("unavailable"))
        for _ in range(2):
            with self.assertRaises(RuntimeError):
                speech.synthesize_native("你好", primary, fallback)
        primary.assert_called_once()
        fallback.assert_called_once()

    def test_long_text_limits_usage_and_discloses_shortened_audio(self):
        text = speech.speech_text("字" * 2000)
        self.assertLess(len(text), 1300)
        self.assertIn("完整內容請看文字", text)

    @patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"})
    @patch("native_speech.subprocess.run")
    @patch("native_speech.urllib.request.urlopen")
    def test_pcm_request_and_actual_duration_conversion(self, urlopen, run):
        urlopen.return_value.__enter__.return_value.read.return_value = b"\0\0" * 24000
        run.return_value = SimpleNamespace(stdout=b"ID3test")
        self.assertEqual(speech.openai_speech("繁體中文"), (b"ID3test", 1000))
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.openai.com/v1/audio/speech")
        body = json.loads(request.data)
        self.assertEqual(body["response_format"], "pcm")
        self.assertEqual(body["input"], "繁體中文")
        self.assertEqual(body["voice"], "coral")
        self.assertIn("24000", run.call_args.args[0])
        self.assertEqual(run.call_args.kwargs["input"], b"\0\0" * 24000)

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""})
    @patch("native_speech.urllib.request.urlopen")
    def test_missing_key_does_not_send_data(self, urlopen):
        with self.assertRaises(RuntimeError):
            speech.openai_speech("private reply")
        urlopen.assert_not_called()

    @patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"})
    @patch("native_speech.urllib.request.urlopen")
    def test_provider_error_does_not_expose_body(self, urlopen):
        urlopen.side_effect = urllib.error.HTTPError("https://api.openai.com", 429, "quota", {}, None)
        with self.assertRaisesRegex(RuntimeError, "HTTP 429"):
            speech.openai_speech("test")


if __name__ == "__main__":
    unittest.main()
