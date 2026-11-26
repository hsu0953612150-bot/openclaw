import unittest
import urllib.parse
from unittest.mock import Mock

from video_knowledge import Cloud, HEADERS


class KnowledgeIndexRetryTests(unittest.TestCase):
    def cloud(self, rows):
        cloud = object.__new__(Cloud)
        cloud.sheet_id = "test-sheet"
        cloud.tab = "影片連結紀錄"
        cloud.api = Mock(side_effect=[{"sheets": [{"properties": {"title": cloud.tab}}]}, {"values": [HEADERS] + rows}, {}])
        return cloud

    def test_completed_retry_updates_only_matching_event_and_preserves_date(self):
        old = ["original-date", "owner", "LINE", "source", "message", "pending", "https://drive.google.com/file/d/file/view", "待處理", "record", "event"]
        other = list(old)
        other[9] = "other-event"
        cloud = self.cloud([other, old])
        cloud.log({"record_id": "record", "source_url": "source", "drive_file_id": "file", "status": "已完成"}, "message", "analysis", "LINE", "owner", "event")
        args = cloud.api.call_args.args
        self.assertIn(urllib.parse.quote("'影片連結紀錄'!A3:J3", safe=""), args[0])
        self.assertEqual(args[2], "PUT")
        row = args[1]["values"][0]
        self.assertEqual(row[0], "original-date")
        self.assertEqual(row[5], "analysis")
        self.assertEqual(row[7:], ["已完成", "record", "event"])
        self.assertNotIn(":append", args[0])

    def test_identical_retry_does_not_write(self):
        old = ["date", "owner", "LINE", "source", "message", "analysis", "https://drive.google.com/file/d/file/view", "已完成", "record", "event"]
        cloud = self.cloud([old])
        cloud.log({"record_id": "record", "source_url": "source", "drive_file_id": "file", "status": "已完成"}, "message", "analysis", "LINE", "owner", "event")
        self.assertEqual(cloud.api.call_count, 2)

    def test_new_event_appends_without_overwriting_existing_event(self):
        old = ["date", "owner", "LINE", "source", "message", "pending", "link", "待處理", "record", "old-event"]
        cloud = self.cloud([old])
        cloud.log({"record_id": "record", "source_url": "source", "drive_file_id": "file", "status": "已完成"}, "message", "analysis", "LINE", "owner", "new-event")
        self.assertIn(":append", cloud.api.call_args.args[0])


if __name__ == "__main__":
    unittest.main()

