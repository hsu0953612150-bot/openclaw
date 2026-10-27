import hashlib
import os
import time
import unittest
from unittest.mock import Mock, patch

from reply_playback import PlaybackStore, final_messages
from codex_line_bridge import deliver_notification
from test_codex_line_bridge import MemoryStore


class FakePlaybackSheet:
    def __init__(self):
        self.rows = []
    def read(self, tab, cells):
        return self.rows if cells == "A2:C" else [["headers"]]
    def append(self, tab, values):
        self.rows.append(values)


@patch.dict(os.environ, {"GOOGLE_OAUTH_REFRESH_TOKEN": "test-only-not-a-real-token"})
class PlaybackTests(unittest.TestCase):
    def test_old_answer_survives_later_replies_and_store_restart(self):
        sheet = FakePlaybackSheet()
        token = PlaybackStore(sheet).save("第一則私人回覆")
        for i in range(12):
            PlaybackStore(sheet).save("之後第" + str(i) + "則回覆")
        self.assertEqual(PlaybackStore(sheet).load(token), "第一則私人回覆")
        self.assertNotIn("私人", str(sheet.rows))
        self.assertNotIn(token, str(sheet.rows))
        self.assertEqual(sheet.rows[0][0], hashlib.sha256(token.encode()).hexdigest())

    def test_bad_expired_or_tampered_tokens_fail_closed(self):
        sheet = FakePlaybackSheet()
        token = PlaybackStore(sheet).save("private")
        for bad in ("bad", "a" * 32):
            with self.assertRaises(LookupError):
                PlaybackStore(sheet).load(bad)
        sheet.rows[0][2] = str(time.time() - 1)
        with self.assertRaises(LookupError):
            PlaybackStore(sheet).load(token)
        sheet.rows[0][2] = str(time.time() + 60)
        sheet.rows[0][1] = "tampered"
        with self.assertRaises(LookupError):
            PlaybackStore(sheet).load(token)

    def test_tts_failure_keeps_text_and_reports_failure_without_web_card(self):
        text = "完整回答🙂" * 4000
        messages = final_messages(text, "https://example.com", PlaybackStore(FakePlaybackSheet()), Mock(side_effect=RuntimeError("quota")))
        self.assertEqual("".join(m["text"] for m in messages[:-1]), text)
        self.assertIn("語音暫時無法產生", messages[-1]["text"])
        self.assertTrue(all(m["type"] == "text" for m in messages))

    def test_native_audio_has_duration_and_no_web_or_sheet_dependency(self):
        store = Mock(side_effect=RuntimeError("must not access Sheets"))
        audio = {"type": "audio", "originalContentUrl": "https://example.com/audio/test.mp3", "duration": 37000}
        messages = final_messages("這是回答", "https://example.com", store, Mock(return_value=audio))
        self.assertEqual([m["type"] for m in messages], ["text", "audio"])
        self.assertEqual(messages[-1], audio)
        self.assertIn("AI合成語音", messages[0]["text"])
        store.save.assert_not_called()

    @patch("codex_line_bridge.line_api")
    def test_long_codex_result_preserves_all_text_batches_and_retry_ids(self, api):
        store = MemoryStore()
        text = "長回答" * 8000
        store.rows.append(["job", "", "owner", "", "", "completed", text, "", "", ""])
        callback = lambda value: final_messages(value, "https://example.com", audio_message=lambda _: {"type": "audio", "originalContentUrl": "https://example.com/audio/test.mp3", "duration": 37000})
        deliver_notification({"kind": "result", "id": "job"}, "test", store, reply_messages=callback)
        calls = api.call_args_list
        self.assertGreater(len(calls), 1)
        self.assertTrue(all(len(c.args[2]["messages"]) <= 5 for c in calls))
        self.assertEqual(len(set(c.args[3] for c in calls)), len(calls))
        sent = [m for c in calls for m in c.args[2]["messages"]]
        self.assertEqual("".join(m["text"] for m in sent if m["type"] == "text"), text + "\n\n任務：job\n\n（附AI合成語音）")
        self.assertEqual(sent[-1]["type"], "audio")
        self.assertTrue(store.rows[0][9])

    def test_encrypted_speech_fits_sheet_cell_limit(self):
        sheet = FakePlaybackSheet()
        token = PlaybackStore(sheet).save("字🙂" * 15000)
        self.assertLess(len(sheet.rows[0][1]), 50000)
        self.assertIn("完整內容", PlaybackStore(sheet).load(token))


if __name__ == "__main__":
    unittest.main()
