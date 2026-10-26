import hashlib
import json
import time
import unittest
from unittest.mock import patch

import codex_line_bridge as bridge


class MemoryStore:
    def __init__(self, owner="owner"):
        self.config = {"enabled": "true", "owner_id": owner, "worker_heartbeat": str(time.time())}
        self.rows = []
        self.prompts = []

    def control(self):
        return dict(self.config)

    def set_control(self, key, value):
        self.config[key] = str(value)

    def tasks(self):
        return [(i + 2, list(row)) for i, row in enumerate(self.rows)]

    def append(self, tab, values):
        (self.rows if tab == bridge.TASKS else self.prompts).append(list(values))

    def questions(self):
        return [(i + 2, list(row)) for i, row in enumerate(self.prompts)]

    def question_update(self, index, row):
        self.prompts[index - 2] = list(row)

    def task_update(self, index, row):
        self.rows[index - 2] = list(row)


class LineBridgeTests(unittest.TestCase):
    def test_notification_requires_signature_and_recent_timestamp(self):
        body = json.dumps({"kind": "probe", "timestamp": time.time()}).encode()
        signature = bridge.notification_signature("example-secret", body)
        self.assertEqual(bridge.verify_notification("example-secret", body, signature)["kind"], "probe")
        for secret, payload, supplied in (("", body, signature), ("different", body, signature), ("example-secret", body + b" ", signature)):
            with self.assertRaises(PermissionError):
                bridge.verify_notification(secret, payload, supplied)
        expired = json.dumps({"kind": "probe", "timestamp": time.time() - 301}).encode()
        with self.assertRaises(PermissionError):
            bridge.verify_notification("example-secret", expired, bridge.notification_signature("example-secret", expired))

    @patch.object(bridge, "line_api")
    def test_delivery_uses_stored_owner_and_result_not_caller_values(self, api):
        store = MemoryStore()
        store.rows = [["job", "0", "owner", "msg", "指令", "completed", "真正結果", "thread", "0", ""]]
        bridge.deliver_notification({"kind": "result", "id": "job", "to": "stranger", "text": "偽造內容"}, "example-token", store)
        sent = api.call_args.args[2]
        self.assertEqual(sent["to"], "owner")
        self.assertIn("真正結果", sent["messages"][0]["text"])
        self.assertNotIn("偽造內容", str(sent))
        self.assertTrue(store.rows[0][9])
        bridge.deliver_notification({"kind": "result", "id": "job"}, "example-token", store)
        api.assert_called_once()

    @patch.object(bridge, "line_api")
    def test_delivery_rejects_unpaired_unfinished_or_wrong_owner_jobs(self, api):
        for owner, task_owner, state in (("", "owner", "completed"), ("owner", "stranger", "completed"), ("owner", "owner", "queued")):
            store = MemoryStore(owner)
            store.rows = [["job", "0", task_owner, "msg", "指令", state, "結果", "", "0", ""]]
            with self.assertRaises((PermissionError, ValueError)):
                bridge.deliver_notification({"kind": "result", "id": "job"}, "example-token", store)
        api.assert_not_called()

    def test_all_owner_text_is_queued_without_prefix(self):
        store = MemoryStore()
        result = bridge.route_text("owner", "幫我整理工作區", "message-1", store=store)
        self.assertIn("Codex", result)
        self.assertEqual(store.rows[0][4:6], ["幫我整理工作區", "queued"])

    def test_other_user_and_group_cannot_operate_pc(self):
        store = MemoryStore()
        self.assertIsNone(bridge.route_text("stranger", "刪除所有檔案", "a", store=store))
        self.assertIsNone(bridge.route_text("owner", "刪除所有檔案", "b", "group", store))
        self.assertEqual(store.rows, [])

    def test_duplicate_webhook_does_not_duplicate_execution(self):
        store = MemoryStore()
        bridge.route_text("owner", "寫入測試檔", "same-id", store=store)
        bridge.route_text("owner", "寫入測試檔", "same-id", store=store)
        self.assertEqual(len(store.rows), 1)

    def test_offline_task_is_retained(self):
        store = MemoryStore()
        store.config["worker_heartbeat"] = "0"
        self.assertIn("離線", bridge.route_text("owner", "檢查專案", "a", store=store))
        self.assertEqual(store.rows[0][5], "queued")

    def test_pairing_code_binds_only_once(self):
        store = MemoryStore(owner="")
        store.config.update(pairing_sha256=hashlib.sha256(b"AABBCCDDEEFF").hexdigest(), pairing_expires=str(time.time() + 60))
        self.assertIn("已綁定", bridge.route_text("owner", "綁定電腦 AABBCCDDEEFF", "a", store=store))
        self.assertEqual(store.config["owner_id"], "owner")
        bridge.route_text("stranger", "綁定電腦 AABBCCDDEEFF", "b", store=store)
        self.assertEqual(store.config["owner_id"], "owner")
        self.assertEqual(store.config["pairing_sha256"], "")

    def test_wrong_expired_or_exhausted_pairing_cannot_bind(self):
        for expiry, attempts in ((time.time() - 1, 0), (time.time() + 60, 20)):
            store = MemoryStore(owner="")
            store.config.update(pairing_sha256=hashlib.sha256(b"AABBCCDDEEFF").hexdigest(), pairing_expires=str(expiry), pairing_attempts=str(attempts))
            bridge.route_text("owner", "綁定電腦 AABBCCDDEEFF", "a", store=store)
            self.assertEqual(store.config["owner_id"], "")

    def test_approval_requires_matching_owner_code_and_pending_state(self):
        store = MemoryStore()
        store.prompts = [["AABBCCDD", "job", "owner", "具體操作", "pending", "", str(time.time()), "approval"]]
        bridge.route_text("stranger", "確認 AABBCCDD", "a", store=store)
        bridge.route_text("owner", "確認 11223344", "b", store=store)
        self.assertEqual(store.prompts[0][4], "pending")
        bridge.route_text("owner", "拒絕 AABBCCDD", "c", store=store)
        self.assertEqual(store.prompts[0][4:6], ["answered", "decline"])
        bridge.route_text("owner", "確認 AABBCCDD", "d", store=store)
        self.assertEqual(store.prompts[0][5], "decline")

    def test_expired_approval_does_not_authorize(self):
        store = MemoryStore()
        store.prompts = [["AABBCCDD", "job", "owner", "操作", "pending", "", str(time.time() - 1801), "approval"]]
        self.assertIn("逾時", bridge.route_text("owner", "確認 AABBCCDD", "a", store=store))
        self.assertEqual(store.prompts[0][4], "pending")

    def test_question_accepts_explicit_answer(self):
        store = MemoryStore()
        store.prompts = [["AABBCCDD", "job", "owner", "哪個資料夾？", "pending", "", str(time.time()), "question"]]
        bridge.route_text("owner", "確認 AABBCCDD", "a", store=store)
        self.assertEqual(store.prompts[0][4], "pending")
        bridge.route_text("owner", "回答 AABBCCDD D:/大G/data", "b", store=store)
        self.assertEqual(store.prompts[0][5], "D:/大G/data")

    def test_owner_cancellation_is_control_not_new_task(self):
        store = MemoryStore()
        store.config["active_job"] = "job-1"
        bridge.route_text("owner", "停止任務", "a", store=store)
        self.assertEqual(store.config["cancel_job"], "job-1")
        self.assertEqual(store.rows, [])

    def test_disabled_bridge_does_not_capture_messages(self):
        store = MemoryStore()
        store.config["enabled"] = "false"
        self.assertIsNone(bridge.route_text("owner", "測試", "a", store=store))

    def test_queue_limit_prevents_unbounded_jobs(self):
        store = MemoryStore()
        for i in range(20):
            bridge.route_text("owner", "測試", str(i), store=store)
        self.assertIn("20筆", bridge.route_text("owner", "第21筆", "21", store=store))
        self.assertEqual(len(store.rows), 20)


if __name__ == "__main__":
    unittest.main()
