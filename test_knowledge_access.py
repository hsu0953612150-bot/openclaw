import ast
import json
from pathlib import Path
from types import SimpleNamespace
import time
import unittest
from unittest.mock import Mock, patch

import knowledge_access as access


class LookupStore:
    def __init__(self):
        self.config = {"owner_id": "owner", "enabled": "true", "worker_heartbeat": str(time.time()), "worker_status": "待命"}
        self.rows = []

    def control(self):
        return self.config

    def read(self, tab, cells):
        return [access.HEADERS] if cells == "A1:H1" else self.rows

    def append(self, tab, row):
        self.rows.append(list(row))
        return {"updates": {"updatedRange": "'tab'!A2:H2"}}

    def write(self, tab, cells, values):
        self.rows[int(cells.split(":")[0][1:]) - 2] = values[0]


class LookupTests(unittest.TestCase):
    def test_offline_and_other_users_do_not_queue_queries(self):
        for user, heartbeat in (("stranger", time.time()), ("owner", 0)):
            store = LookupStore()
            store.config["worker_heartbeat"] = str(heartbeat)
            with patch.object(access.os, "name", "posix"):
                self.assertEqual(access.retrieve_local_context(user, "storage policy", store, .02), {})
            self.assertEqual(store.rows, [])

    def test_online_query_round_trip_returns_only_data(self):
        store = LookupStore()
        result = {"text": "local-result", "source": "local"}
        def poll(_):
            access.process_local_lookups(store, "owner")
        access._cache.clear()
        with patch.object(access.os, "name", "posix"), patch.object(access.time, "sleep", side_effect=poll), patch("cloud_backup.search_local", return_value=result):
            self.assertEqual(access.retrieve_local_context("owner", "unique storage question", store, .5), result)
        self.assertEqual(store.rows[0][4], "done")

    def test_expired_wrong_owner_and_malformed_requests_are_never_searched(self):
        store = LookupStore()
        for owner, expiry in (("stranger", time.time()+60), ("owner", 0), ("owner", "invalid")):
            store.rows.append(["id", "0", owner, "query", "queued", str(expiry), "", ""])
        with patch("cloud_backup.search_local") as search:
            access.process_local_lookups(store, "owner")
            search.assert_not_called()
        self.assertTrue(all(row[4] == "expired" for row in store.rows))

    def test_app_local_match_skips_cloud_knowledge_without_replacing_fresh_conversation(self):
        source = ast.parse((Path(__file__).parent / "app.py").read_text(encoding="utf-8"))
        node = next(n for n in source.body if isinstance(n, ast.FunctionDef) and n.name == "process_message")
        local, cloud, live, drive = Mock(return_value={"text": "D槽資料"}), Mock(), Mock(), Mock()
        load = Mock(return_value=([{"role": "assistant", "content": "fresh history"}], {}))
        generate = Mock(return_value="回答")
        ns = {"retrieve_local_context": local, "retrieve_video_knowledge": cloud, "retrieve_liveboard_memory": live, "retrieve_knowledge": drive, "load_user_context": load,
              "extract_urls": lambda _: [], "retry_sources_from_history": lambda *_: [], "update_profile_from_text": lambda p, _: p,
              "append_liveboard_memory": Mock(), "save_user_context": Mock(), "send_voice_reply": Mock(), "send_text": Mock(),
              "gemini_generate": generate, "GeminiQuotaError": RuntimeError, "app": SimpleNamespace(logger=Mock()), "re": __import__("re")}
        exec(compile(ast.Module(body=[node], type_ignores=[]), "local_retrieval", "exec"), ns)
        ns["process_message"]("owner", "資料保存規則")
        local.assert_called_once()
        load.assert_called_once()
        for call in (cloud, live, drive):
            call.assert_not_called()
        self.assertIn("D槽資料", generate.call_args.kwargs["knowledge_text"])
        self.assertEqual(generate.call_args.args[0][0]["content"], "fresh history")


if __name__ == "__main__":
    unittest.main()

