import time
import ast
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from message_routing import needs_computer, route_classified_text
from line_voice_bridge import route_audio_to_codex
from test_codex_line_bridge import MemoryStore


class RoutingTests(unittest.TestCase):
    def test_link_and_file_knowledge_final_replies_also_have_playback(self):
        source = ast.parse((Path(__file__).parent / "app.py").read_text(encoding="utf-8"))
        node = next(n for n in source.body if isinstance(n, ast.FunctionDef) and n.name == "process_message")
        for args in ({"user_text": "https://example.com"}, {"file_data": b"file", "filename": "test.txt"}):
            voice, plain = Mock(), Mock()
            namespace = {"load_user_context": lambda _: ([], {}), "update_profile_from_text": lambda p, _: p, "append_liveboard_memory": Mock(), "extract_urls": lambda _: ["https://example.com"] if "user_text" in args else [], "ingest_resource": Mock(return_value={"answer": "內容已分析及保存"}), "save_user_context": Mock(), "send_voice_reply": voice, "send_text": plain, "app": SimpleNamespace(logger=Mock())}
            exec(compile(ast.Module(body=[node], type_ignores=[]), "knowledge_final_reply", "exec"), namespace)
            namespace["process_message"]("owner", **args)
            voice.assert_called_once_with("owner", "內容已分析及保存")
            plain.assert_not_called()

    def test_questions_greetings_links_and_cloud_knowledge_do_not_use_pc(self):
        for text in ("在嗎？", "有聽得到我說話嗎？", "Chrome是什麼？", "如何打開Chrome", "今天台北天氣如何", "幫我查台北到香港機票", "https://youtube.com/shorts/example", "分析這個影片並存入大G知識庫", "不要打開Chrome，只告訴我怎麼做", "幫我解釋電腦記憶體"):
            with self.subTest(text=text):
                self.assertFalse(needs_computer(text, active=True))
                store = Mock()
                self.assertIsNone(route_classified_text("owner", text, "id", store=store))
                store.control.assert_not_called()

    def test_explicit_computer_actions_are_queued(self):
        for text in ("打開Google Chrome瀏覽器", "開啟YouTube首頁", "幫我修改這個程式", "檢查這台電腦記憶體", "將檔案移動到D:/大G", "請用電腦處理這份報告", "幫我部署", "登入Trip網站"):
            with self.subTest(text=text):
                store = MemoryStore()
                self.assertIsNotNone(route_classified_text("owner", text, "id", store=store))
                self.assertEqual(len(store.rows), 1)

    def test_only_bound_owner_private_messages_can_queue(self):
        for user, source in (("other", "user"), ("owner", "group")):
            store = MemoryStore()
            self.assertIsNone(route_classified_text(user, "打開Chrome", "id", source, store))
            self.assertEqual(store.rows, [])

    def test_followups_need_active_task_but_new_question_stays_cloud(self):
        store = MemoryStore()
        self.assertIsNone(route_classified_text("owner", "請繼續", "id", store=store))
        store.config["active_job"] = "active"
        self.assertIsNotNone(route_classified_text("owner", "請繼續", "id", store=store))
        self.assertIsNone(route_classified_text("owner", "今天天氣如何", "id2", store=store))
        self.assertEqual(len(store.rows), 1)

    def test_valid_typed_approval_bypasses_classifier_without_queue(self):
        store = MemoryStore()
        store.prompts.append(["ABCD1234", "job", "owner", "允許嗎", "pending", "", str(time.time()), "approval"])
        route_classified_text("owner", "確認 ABCD1234", "id", store=store)
        self.assertEqual(store.prompts[0][5], "accept")
        self.assertEqual(store.rows, [])

    def test_voice_chat_uses_same_classifier_even_when_queue_full(self):
        store = MemoryStore()
        store.rows = [[str(i), "", "owner", "", "", "queued", "", "", "", ""] for i in range(20)]
        chat = Mock()
        response = route_audio_to_codex("owner", "new", lambda: b"audio", store=store, transcribe=lambda *_: "有聽得到我說話嗎？", normal_chat=chat)
        self.assertEqual(response, "")
        chat.assert_called_once_with("有聽得到我說話嗎？")
        self.assertEqual(len(store.rows), 20)


if __name__ == "__main__":
    unittest.main()
