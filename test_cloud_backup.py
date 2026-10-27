import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.parse

import cloud_backup as backup


class BackupCloud:
    folder_id = "folder"
    sheet_id = "sheet"

    def __init__(self):
        self.data = b"cloud primary local first: storage policy"
        self.version = "1"
        self.uploads = []

    def api(self, url, *args, **kwargs):
        if "drive/v3/files?" in url:
            return {"files": [{"id": "doc", "name": "storage-policy.txt", "mimeType": "text/plain", "size": str(len(self.data)), "version": self.version, "md5Checksum": hashlib.md5(self.data).hexdigest()}]}
        if "values:batchGet" in url:
            return {"valueRanges": [{"values": [["owner", "date", json.dumps([{"role": "user", "content": "private-memory-marker"}]), "", "{}"]]}, {"values": [["other", "data"]]}]}
        return {"properties": {"title": "Existing"}, "sheets": [{"properties": {"title": "UserMemory"}}, {"properties": {"title": "Other"}}]}

    def list_files(self, *args, **kwargs):
        return [{"id": "generated-index"}] if self.uploads else []

    def upload(self, name, data, mime, properties, file_id=None):
        self.uploads.append((name, data, properties, file_id))
        return {"id": "generated-index"}

    def fetch(self, cloud, item, target):
        target.write_bytes(self.data)
        return len(self.data)


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cloud = BackupCloud()

    def sync(self, fetch=None):
        return backup.sync_once(self.cloud, self.root, fetch or self.cloud.fetch)

    def test_incremental_verification_preserves_versions_and_all_sheet_tabs(self):
        first = self.sync()
        self.assertTrue(first["complete"])
        self.assertEqual(first["downloaded"], 1)
        self.assertEqual(set(backup.read_json(self.root / "sheets/latest.json", {})["tabs"]), {"UserMemory", "Other"})
        again = self.sync()
        self.assertEqual(again["downloaded"], 0)
        self.assertEqual(again["unchanged"], 1)
        self.assertEqual(len(self.cloud.uploads), 1)
        old = self.cloud.data
        self.cloud.data = b"cloud primary local first: revised policy"
        self.cloud.version = "2"
        changed = self.sync()
        self.assertTrue(changed["complete"])
        copies = list((self.root / "versions/doc").glob("*/*.txt"))
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies[0].read_bytes(), old)
        self.assertEqual(len(self.cloud.uploads), 2)

    def test_corrupt_download_never_replaces_good_copy(self):
        self.sync()
        old = (self.root / "files/doc/storage-policy.txt").read_bytes()
        self.cloud.version = "2"
        def corrupt(cloud, item, target):
            target.write_bytes(b"corrupt")
            return 7
        result = self.sync(corrupt)
        self.assertFalse(result["complete"])
        self.assertEqual((self.root / "files/doc/storage-policy.txt").read_bytes(), old)
        self.assertEqual(backup.read_json(self.root / "manifest.json", {})["files"]["doc"]["version"], "1")

    def test_deleted_cloud_file_is_retained_but_no_longer_searchable(self):
        self.sync()
        with patch.object(backup, "drive_files", return_value=[]):
            self.sync()
        self.assertTrue((self.root / "files/doc/storage-policy.txt").exists())
        self.assertEqual(backup.search_local("storage policy", self.root), {})

    def test_owner_memory_isolated_and_tampered_file_not_published(self):
        self.sync()
        self.assertIn("private-memory-marker", backup.search_local("private-memory-marker", self.root, "owner")["text"])
        self.assertEqual(backup.search_local("private-memory-marker", self.root, "stranger"), {})
        (self.root / "files/doc/storage-policy.txt").write_text("tampered content", encoding="utf-8")
        self.assertEqual(backup.build_index(self.root)["documents"], [])

    def test_backup_paths_cannot_escape_and_index_is_not_recursively_ingested(self):
        with self.assertRaises(ValueError):
            backup.contained(self.root, "../outside")
        class Paged:
            folder_id = "root"
            def api(self, url):
                params = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
                if "'sub'" in params["q"][0]:
                    return {"files": [{"id": "nested", "mimeType": "text/plain"}]}
                if "pageToken" in params:
                    return {"files": [{"id": "sub", "mimeType": "application/vnd.google-apps.folder"}]}
                return {"nextPageToken": "page2", "files": [{"id": "index", "appProperties": {"daga_role": "lookup-index"}}, {"id": "first", "mimeType": "text/plain"}]}
        self.assertEqual([f["id"] for f in backup.drive_files(Paged())], ["first", "nested"])


if __name__ == "__main__":
    unittest.main()

