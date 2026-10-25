import json
import unittest
from unittest.mock import patch

import video_knowledge as workflow


class FakeCloud:
    def __init__(self, existing=None, fail_log=False):
        self.record = existing
        self.fail_log = fail_log
        self.saved = []
        self.logged = []

    def check(self):
        return {}

    def find_record(self, record_id):
        return self.record

    def save(self, record):
        copy = json.loads(json.dumps(record))
        copy["drive_file_id"] = "test-master-id"
        self.saved.append(json.loads(json.dumps(copy)))
        self.record = copy
        return copy

    def log(self, *args):
        if self.fail_log:
            raise RuntimeError("Sheets unavailable")
        self.logged.append(args)


ANALYSIS = {"title": "測試影片", "summary": "影片摘要", "knowledge_points": ["影片知識點"]}
URL = "https://www.youtube.com/watch?v=ebFYTlePxx4"


class WorkflowTests(unittest.TestCase):
    def test_shorts_and_share_links_have_same_identity(self):
        for url in ("https://youtube.com/shorts/ebFYTlePxx4?si=share", "https://youtu.be/ebFYTlePxx4?t=8", URL):
            self.assertEqual(workflow.canonical_url(url), URL)

    def test_unknown_web_link_queued_but_ordinary_reference_not_intercepted(self):
        self.assertEqual(workflow.extract_video_urls("https://example.com/watch/1"), ["https://example.com/watch/1"])
        self.assertEqual(workflow.extract_video_urls("請比較這份文件 https://example.com/docs"), [])
        self.assertEqual(workflow.extract_video_urls("https://youtube.com.evil.example/watch?v=ebFYTlePxx4"), ["https://youtube.com.evil.example/watch?v=ebFYTlePxx4"])
        self.assertEqual(workflow.canonical_url("https://youtube.com.evil.example/watch?v=ebFYTlePxx4"), "https://youtube.com.evil.example/watch?v=ebFYTlePxx4")

    @patch.object(workflow, "_analyze", return_value=ANALYSIS)
    def test_analysis_and_master_saved_before_index(self, analyze):
        cloud = FakeCloud()
        result = workflow.ingest(URL, "owner", cloud=cloud)
        self.assertEqual(result["status"], "已完成")
        self.assertEqual([r["status"] for r in cloud.saved], ["待處理", "已完成"])
        self.assertTrue(result["index_saved"])
        analyze.assert_called_once()

    @patch.object(workflow, "_analyze", side_effect=RuntimeError("模型未能讀取影片內容"))
    def test_unreadable_video_has_no_fabricated_summary(self, _):
        cloud = FakeCloud()
        result = workflow.ingest(URL, "owner", cloud=cloud)
        self.assertEqual(result["status"], "待處理")
        self.assertNotIn("analysis", cloud.record)
        self.assertIn("待處理", result["answer"])

    @patch.object(workflow, "_analyze", return_value=ANALYSIS)
    def test_index_failure_does_not_claim_full_success_and_retry_skips_analysis(self, analyze):
        cloud = FakeCloud(fail_log=True)
        result = workflow.ingest(URL, "owner", cloud=cloud)
        self.assertFalse(result["index_saved"])
        self.assertIn("Sheets對話索引寫入失敗", result["answer"])
        cloud.fail_log = False
        retry = workflow.ingest(URL, "owner", cloud=cloud)
        self.assertTrue(retry["index_saved"])
        self.assertEqual(analyze.call_count, 1)

    @patch.object(workflow, "_analyze", return_value=ANALYSIS)
    def test_owner_ids_are_isolated_without_explicit_binding(self, _):
        one = workflow.ingest(URL, "owner-a", cloud=FakeCloud())
        two = workflow.ingest(URL, "owner-b", cloud=FakeCloud())
        self.assertNotEqual(one["record_id"], two["record_id"])


if __name__ == "__main__":
    unittest.main()
