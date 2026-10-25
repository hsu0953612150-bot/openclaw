import io
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import knowledge_intake as intake
from test_video_knowledge import FakeCloud


ANALYSIS = {"title": "測試素材", "summary": "摘要", "knowledge_points": ["可調用的知識"], "category": "科技與開發"}


class ArchiveCloud(FakeCloud):
    def __init__(self, deny=False, fail_log=False):
        super().__init__(fail_log=fail_log)
        self.deny = deny
        self.uploads = []

    def check(self):
        if self.deny:
            raise RuntimeError("尚未完成Google授權")
        return {}

    def upload(self, name, data, mime):
        self.uploads.append((name, bytes(data), mime))
        return {"id": "test-asset"}


class IntakeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {"KNOWLEDGE_LOCAL_DIR": self.temp.name, "VIDEO_KNOWLEDGE_OWNER_ID": ""})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_markdown_link_and_generic_webpage_are_detected(self):
        self.assertEqual(intake.extract_urls("請打開[資料](https://example.com/guide)"), ["https://example.com/guide"])
        self.assertFalse(intake.video_site("https://youtube.com.example.org/watch?v=123"))

    def test_private_addresses_are_rejected(self):
        for address in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "::1"):
            with patch.object(socket, "getaddrinfo", return_value=[(0, 0, 0, "", (address, 443))]):
                with self.assertRaises(RuntimeError):
                    intake.public_url("https://example.com/private")

    def test_redirect_to_private_destination_is_rejected(self):
        with patch.object(socket, "getaddrinfo", return_value=[(0, 0, 0, "", ("127.0.0.1", 80))]):
            with self.assertRaises(RuntimeError):
                intake.PublicRedirect().redirect_request(None, None, 302, "", {}, "http://localhost/")

    def test_html_scripts_not_treated_as_article_text(self):
        text, partial = intake.document_text("page.html", b"<h1>Article</h1><script>hidden()</script><p>Knowledge</p>", "text/html")
        self.assertIn("Knowledge", text)
        self.assertNotIn("hidden", text)
        self.assertFalse(partial)

    def test_docx_is_parsed_without_opening_an_application(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("word/document.xml", '<w:document xmlns:w="urn:test"><w:p><w:t>Knowledge</w:t></w:p></w:document>')
        text, _ = intake.document_text("sample.docx", buffer.getvalue(), "application/octet-stream")
        self.assertIn("Knowledge", text)

    def test_pptx_slide_order_is_preserved(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for index in (10, 2, 1):
                archive.writestr(f"ppt/slides/slide{index}.xml", f'<a:slide xmlns:a="urn:test"><a:t>slide-{index}</a:t></a:slide>')
        text, partial = intake.document_text("sample.pptx", buffer.getvalue(), "application/octet-stream")
        self.assertLess(text.index("slide-1"), text.index("slide-2"))
        self.assertLess(text.index("slide-2"), text.index("slide-10"))
        self.assertFalse(partial)

    def test_xlsx_cell_values_are_extracted(self):
        import openpyxl
        workbook = openpyxl.Workbook()
        workbook.active.append(["測試代號", "SHEET8245"])
        buffer = io.BytesIO()
        workbook.save(buffer)
        workbook.close()
        text, partial = intake.document_text("sample.xlsx", buffer.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        self.assertIn("SHEET8245", text)
        self.assertFalse(partial)

    def test_pdf_text_is_extracted_and_scanned_pdf_stays_pending(self):
        import pymupdf as fitz
        with fitz.open() as document:
            page = document.new_page()
            page.insert_text((72, 72), "PDF8245 Knowledge")
            text_data = document.tobytes()
        text, partial = intake.document_text("sample.pdf", text_data, "application/pdf")
        self.assertIn("PDF8245", text)
        self.assertFalse(partial)
        with fitz.open() as document:
            document.new_page()
            blank_data = document.tobytes()
        with self.assertRaisesRegex(RuntimeError, "沒有讀到可用文字"):
            intake.model_input("scan.pdf", blank_data, "application/pdf")

    def test_empty_office_documents_do_not_count_section_labels_as_content(self):
        for extension, member in [("docx", "word/document.xml"), ("pptx", "ppt/slides/slide1.xml")]:
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as archive:
                archive.writestr(member, '<document/>')
            with self.assertRaisesRegex(RuntimeError, "沒有讀到可用文字"):
                intake.model_input("empty." + extension, buffer.getvalue(), "application/octet-stream")
        import openpyxl
        workbook = openpyxl.Workbook()
        buffer = io.BytesIO()
        workbook.save(buffer)
        workbook.close()
        with self.assertRaisesRegex(RuntimeError, "沒有讀到可用文字"):
            intake.model_input("empty.xlsx", buffer.getvalue(), "application/octet-stream")

    @patch.object(intake, "_analyze", return_value=dict(ANALYSIS))
    def test_missing_cloud_auth_preserves_original_and_analysis_then_retry_syncs(self, analyze):
        cloud = ArchiveCloud(deny=True)
        first = intake.ingest("LINE附件：notes.txt", "owner", data=b"Knowledge", filename="notes.txt", cloud=cloud)
        self.assertFalse(first["cloud_saved"])
        record_path = Path(first["local_record"])
        record = json.loads(record_path.read_text(encoding="utf-8"))
        self.assertEqual((record_path.parent / record["local_asset"]).read_bytes(), b"Knowledge")
        self.assertIn("analysis", record)
        self.assertIn("尚未寫入大G雲端", first["answer"])
        cloud.deny = False
        retry = intake.ingest("LINE附件：notes.txt", "owner", data=b"Knowledge", filename="notes.txt", cloud=cloud)
        self.assertTrue(retry["cloud_saved"])
        self.assertEqual(analyze.call_count, 1)
        self.assertEqual(len(cloud.uploads), 1)

    @patch.object(intake, "_analyze")
    def test_unsupported_file_is_retained_without_fabricated_analysis(self, analyze):
        result = intake.ingest("unsupported.zip", "owner", data=b"unknown", filename="unsupported.zip", cloud=ArchiveCloud(deny=True))
        record = json.loads(Path(result["local_record"]).read_text(encoding="utf-8"))
        self.assertEqual(record["status"], "待處理")
        self.assertNotIn("analysis", record)
        analyze.assert_not_called()

    @patch.object(intake, "_analyze", return_value=dict(ANALYSIS))
    @patch.object(intake, "fetch", return_value=("guide.html", b"<p>Knowledge</p>", "text/html", "https://example.com/guide"))
    def test_webpage_is_analyzed_as_text_and_archived_with_original(self, fetch, analyze):
        cloud = ArchiveCloud()
        result = intake.ingest("https://example.com/guide", "owner", cloud=cloud)
        self.assertTrue(result["cloud_saved"])
        self.assertEqual(analyze.call_args.args[0]["type"], "text")
        self.assertEqual(cloud.uploads[0][1], b"<p>Knowledge</p>")
        self.assertIn("分類：科技與開發", result["answer"])

    @patch.object(intake, "_analyze", return_value=dict(ANALYSIS))
    def test_sheet_failure_keeps_successful_drive_result(self, _):
        result = intake.ingest("notes.txt", "owner", data=b"Knowledge", filename="notes.txt", cloud=ArchiveCloud(fail_log=True))
        self.assertTrue(result["cloud_saved"])
        self.assertFalse(result["index_saved"])
        self.assertIn("Sheets索引尚未寫入", result["answer"])

    def test_partial_text_is_labeled(self):
        content, partial = intake.model_input("notes.txt", b"a" * (intake.MAX_TEXT + 1), "text/plain")
        self.assertTrue(partial)
        self.assertIn("僅部分內容", content["text"])

    @patch.object(intake, "_analyze", return_value=dict(ANALYSIS))
    def test_queued_material_can_sync_without_resending_or_reanalysis(self, analyze):
        intake.ingest("notes.txt", "owner", data=b"Knowledge", filename="notes.txt", cloud=ArchiveCloud(deny=True))
        results = intake.sync_pending("owner", cloud=ArchiveCloud())
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["cloud_saved"])
        self.assertEqual(analyze.call_count, 1)

    @patch.object(intake, "_analyze", return_value=dict(ANALYSIS))
    def test_local_knowledge_is_searchable_and_owner_isolated(self, _):
        intake.ingest("notes.txt", "owner", data=b"Knowledge", filename="notes.txt", cloud=ArchiveCloud(deny=True))
        with patch.object(intake, "retrieve_cloud", return_value=""):
            self.assertIn("測試素材", intake.retrieve("owner", "知識"))
            self.assertEqual(intake.retrieve("someone-else", "知識"), "")


if __name__ == "__main__":
    unittest.main()
