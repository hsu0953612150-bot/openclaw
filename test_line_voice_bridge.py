import ast
import base64
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import line_voice_bridge as voice
from test_codex_line_bridge import MemoryStore


class VoiceBridgeTests(unittest.TestCase):
    def test_owner_voice_command_enters_same_pc_queue_as_text(self):
        for transcript in ("打開Google Chrome瀏覽器", "開啟YouTube首頁"):
            store = MemoryStore()
            load_audio = Mock(return_value=b"sample-audio")
            transcribe = Mock(return_value=transcript)
            response = voice.route_audio_to_codex("owner", "line-audio-id", load_audio, duration_ms=3000, store=store, transcribe=transcribe)
            self.assertIn(transcript, response)
            self.assertEqual(store.rows[0][4:6], [transcript, "queued"])
            self.assertEqual(store.rows[0][3], "audio:line-audio-id")
            transcribe.assert_called_once_with(base64.b64encode(b"sample-audio").decode(), "audio/m4a")

    def test_other_users_group_and_disabled_bridge_are_not_transcribed(self):
        for user, source, enabled in (("stranger", "user", "true"), ("owner", "group", "true"), ("owner", "user", "false")):
            store = MemoryStore()
            store.config["enabled"] = enabled
            load, transcribe = Mock(), Mock()
            self.assertIsNone(voice.route_audio_to_codex(user, "id", load, source, store=store, transcribe=transcribe))
            load.assert_not_called()
            transcribe.assert_not_called()

    def test_redelivered_audio_never_retranscribes_or_requeues_completed_job(self):
        store = MemoryStore()
        transcribe = Mock(return_value="開啟YouTube首頁")
        voice.route_audio_to_codex("owner", "same", lambda: b"audio", store=store, transcribe=transcribe)
        store.rows[0][5] = "completed"
        duplicate_load = Mock()
        self.assertIn("不會重複執行", voice.route_audio_to_codex("owner", "same", duplicate_load, store=store, transcribe=transcribe))
        duplicate_load.assert_not_called()
        transcribe.assert_called_once()
        self.assertEqual(len(store.rows), 1)

    def test_long_or_empty_audio_is_not_transcribed(self):
        store = MemoryStore()
        transcribe, load = Mock(), Mock(return_value=b"")
        voice.route_audio_to_codex("owner", "long", load, duration_ms=120001, store=store, transcribe=transcribe)
        load.assert_not_called()
        voice.route_audio_to_codex("owner", "empty", load, store=store, transcribe=transcribe)
        transcribe.assert_not_called()
        self.assertEqual(store.rows, [])

    def test_transcription_failure_does_not_create_an_execution(self):
        store = MemoryStore()
        with self.assertRaises(RuntimeError):
            voice.route_audio_to_codex("owner", "id", lambda: b"audio", store=store, transcribe=Mock(side_effect=RuntimeError("upstream error")))
        self.assertEqual(store.rows, [])

    def test_voice_approval_is_not_accepted_as_a_typed_approval(self):
        store = MemoryStore()
        response = voice.route_audio_to_codex("owner", "id", lambda: b"audio", store=store, transcribe=lambda *_: "確認 ABCD1234")
        self.assertIn("用文字", response)
        self.assertEqual(store.rows, [])

    def test_voice_status_with_punctuation_does_not_queue_an_extra_task(self):
        store = MemoryStore()
        response = voice.route_audio_to_codex("owner", "id", lambda: b"audio", store=store, transcribe=lambda *_: "電腦狀態。")
        self.assertIn("連線正常", response)
        self.assertEqual(store.rows, [])

    def test_binding_change_during_transcription_is_fail_closed(self):
        store = MemoryStore()
        def transcribe(*_):
            store.config["owner_id"] = "stranger"
            return "開啟Chrome"
        self.assertIn("沒有交給電腦執行", voice.route_audio_to_codex("owner", "id", lambda: b"audio", store=store, transcribe=transcribe))
        self.assertEqual(store.rows, [])

    def test_flask_audio_adapter_does_not_fall_back_to_chat_for_owner(self):
        source = ast.parse((Path(__file__).parent / "app.py").read_text(encoding="utf-8"))
        node = next(item for item in source.body if isinstance(item, ast.FunctionDef) and item.name == "process_audio_message")
        route = Mock(return_value="已交給電腦Codex")
        send, chat = Mock(), Mock()
        namespace = {"route_audio_to_codex": route, "send_text": send, "process_message": chat, "app": SimpleNamespace(logger=Mock()), "base64": base64}
        exec(compile(ast.Module(body=[node], type_ignores=[]), "audio_adapter", "exec"), namespace)
        namespace["process_audio_message"]("owner", "message-id", "user", 5000)
        chat.assert_not_called()
        send.assert_called_once_with("owner", "已交給電腦Codex")
        self.assertEqual(route.call_args.args[3:], ("user", 5000))

    def test_adapter_error_never_becomes_an_unexecuted_success_reply(self):
        source = ast.parse((Path(__file__).parent / "app.py").read_text(encoding="utf-8"))
        node = next(item for item in source.body if isinstance(item, ast.FunctionDef) and item.name == "process_audio_message")
        send, chat = Mock(), Mock()
        namespace = {"route_audio_to_codex": Mock(side_effect=RuntimeError("unavailable")), "send_text": send, "process_message": chat, "app": SimpleNamespace(logger=Mock()), "base64": base64}
        exec(compile(ast.Module(body=[node], type_ignores=[]), "audio_adapter", "exec"), namespace)
        namespace["process_audio_message"]("owner", "message-id")
        chat.assert_not_called()
        self.assertIn("尚未確認電腦執行", send.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
