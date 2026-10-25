"""Read a narrowly scoped Google Drive folder as a small Gemini knowledge base."""

import base64
import io
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import xml.etree.ElementTree as ET

import fitz
import openpyxl
from docx import Document
from pptx import Presentation
from oauth2client.service_account import ServiceAccountCredentials


DRIVE_READ_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
MAX_FILES = max(1, min(50, int(os.getenv("GOOGLE_DRIVE_MAX_FILES", "25"))))
MAX_FILE_BYTES = max(1, int(os.getenv("GOOGLE_DRIVE_MAX_FILE_MB", "12"))) * 1024 * 1024
MAX_INDEX_CHARS_PER_FILE = 50_000
MAX_CONTEXT_CHARS = 18_000
MAX_PDF_INLINE_BYTES = 8 * 1024 * 1024
CACHE_SECONDS = 180

_cache_lock = threading.Lock()
_cache = {"loaded_at": 0, "folder_id": "", "files": []}


def _service_account_info():
    raw = os.getenv("GCP_SERVICE_ACCOUNT_JSON", "")
    if not raw:
        raise RuntimeError("GCP_SERVICE_ACCOUNT_JSON is not configured")
    return json.loads(raw.strip())


def _access_token():
    credentials = ServiceAccountCredentials.from_json_keyfile_dict(
        _service_account_info(), [DRIVE_READ_SCOPE]
    )
    return credentials.get_access_token().access_token


def _drive_request(path, token, params=None):
    url = f"https://www.googleapis.com/drive/v3/{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request, timeout=25) as response:
        return response.read()


def _list_files(folder_id, token):
    files = []
    page_token = None
    while len(files) < MAX_FILES:
        params = {
            "q": f"'{folder_id}' in parents and trashed = false",
            "pageSize": min(100, MAX_FILES - len(files)),
            "fields": "nextPageToken,files(id,name,mimeType,size,modifiedTime)",
            "orderBy": "name",
        }
        if page_token:
            params["pageToken"] = page_token
        result = json.loads(_drive_request("files", token, params).decode("utf-8"))
        files.extend(result.get("files", []))
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    return [item for item in files if item.get("mimeType") != "application/vnd.google-apps.folder"]


def _read_file(file_id, token, mime_type):
    encoded_id = urllib.parse.quote(file_id, safe="")
    if mime_type.startswith("application/vnd.google-apps."):
        export_type = {
            "application/vnd.google-apps.document": "text/plain",
            "application/vnd.google-apps.spreadsheet": "text/csv",
            "application/vnd.google-apps.presentation": "text/plain",
            "application/vnd.google-apps.drawing": "application/pdf",
        }.get(mime_type)
        if not export_type:
            return b""
        return _drive_request(
            f"files/{encoded_id}/export", token, {"mimeType": export_type}
        )
    return _drive_request(f"files/{encoded_id}", token, {"alt": "media"})


def _xml_text(data, member, tag_names):
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            root = ET.fromstring(archive.read(member))
    except (KeyError, ET.ParseError, zipfile.BadZipFile):
        return ""
    selected = []
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] in tag_names and element.text:
            value = element.text.strip()
            if value:
                selected.append(value)
    return "\n".join(selected)


def _extract_text(name, mime_type, data):
    extension = os.path.splitext(name.lower())[1]
    if mime_type == "application/pdf" or extension == ".pdf":
        try:
            with fitz.open(stream=data, filetype="pdf") as pdf:
                return "\n".join(page.get_text("text") for page in pdf[:120])
        except Exception:
            return ""
    if extension == ".docx":
        try:
            document = Document(io.BytesIO(data))
            blocks = [paragraph.text for paragraph in document.paragraphs if paragraph.text]
            for table in document.tables:
                blocks.extend(" | ".join(cell.text for cell in row.cells) for row in table.rows)
            return "\n".join(blocks)
        except Exception:
            return ""
    if extension == ".xlsx":
        try:
            workbook = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
            lines = []
            for sheet in workbook.worksheets[:10]:
                lines.append(f"工作表：{sheet.title}")
                for row in sheet.iter_rows(max_row=250, values_only=True):
                    values = [str(value).strip() for value in row if value is not None and str(value).strip()]
                    if values:
                        lines.append(" | ".join(values))
            workbook.close()
            return "\n".join(lines)
        except Exception:
            return ""
    if extension == ".pptx":
        try:
            presentation = Presentation(io.BytesIO(data))
            lines = []
            for slide in presentation.slides:
                for shape in slide.shapes:
                    if getattr(shape, "has_text_frame", False) and shape.text.strip():
                        lines.append(shape.text.strip())
                    if getattr(shape, "has_table", False):
                        for row in shape.table.rows:
                            lines.append(" | ".join(cell.text.strip() for cell in row.cells))
            return "\n".join(lines)
        except Exception:
            return ""
    if extension == ".doc":
        return ""
    if extension in (".txt", ".md", ".csv", ".tsv", ".json", ".html", ".htm", ".xml") or mime_type.startswith("text/"):
        text = data.decode("utf-8-sig", errors="replace")
        if extension in (".html", ".htm"):
            text = re.sub(r"<[^>]+>", " ", text)
        return text
    if extension == ".rtf":
        return re.sub(r"\\[a-z]+\d* ?|[{}]", " ", data.decode("utf-8", errors="replace"))
    return ""


def _split_text(text, chunk_size=1200):
    text = re.sub(r"\s+", " ", text).strip()
    return [text[index:index + chunk_size] for index in range(0, len(text), chunk_size) if text[index:index + chunk_size].strip()]


def _terms(text):
    text = (text or "").lower()
    result = set(re.findall(r"[a-z0-9_]{2,}", text))
    for run in re.findall(r"[\u3400-\u9fff]+", text):
        if len(run) == 1:
            result.add(run)
        else:
            result.update(run[index:index + 2] for index in range(len(run) - 1))
    return result


def _score(query_terms, name, chunk):
    if not query_terms:
        return 0
    title_terms = _terms(name)
    chunk_terms = _terms(chunk)
    return 3 * len(query_terms & title_terms) + len(query_terms & chunk_terms)


def _load_index(folder_id):
    now = time.time()
    with _cache_lock:
        if _cache["folder_id"] == folder_id and now - _cache["loaded_at"] < CACHE_SECONDS:
            return _cache["files"]
    token = _access_token()
    metadata = _list_files(folder_id, token)
    indexed = []
    remaining_bytes = MAX_FILE_BYTES * 2
    for item in metadata:
        try:
            size = int(item.get("size", 0))
        except (TypeError, ValueError):
            size = 0
        if size and size > MAX_FILE_BYTES:
            continue
        if size and size > remaining_bytes:
            continue
        try:
            data = _read_file(item["id"], token, item.get("mimeType", ""))
            if len(data) > MAX_FILE_BYTES or len(data) > remaining_bytes:
                continue
            remaining_bytes -= len(data)
            text = _extract_text(item.get("name", ""), item.get("mimeType", ""), data)
            is_pdf = item.get("mimeType") == "application/pdf" or item.get("name", "").lower().endswith(".pdf")
            if text or is_pdf:
                indexed.append({
                    "id": item["id"],
                    "name": item.get("name", "未命名文件"),
                    "mime_type": item.get("mimeType", "application/octet-stream"),
                    "modified_time": item.get("modifiedTime", ""),
                    "chunks": _split_text(text[:MAX_INDEX_CHARS_PER_FILE]) or [item.get("name", "未命名文件")],
                    "has_text": bool(text),
                    "is_pdf": is_pdf,
                })
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
            continue
    with _cache_lock:
        _cache.update({"loaded_at": now, "folder_id": folder_id, "files": indexed})
    return indexed


def retrieve_knowledge(query):
    """Return relevant document excerpts and at most one native PDF attachment."""
    folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID", "").strip()
    if not folder_id:
        return "", []
    files = _load_index(folder_id)
    if not files:
        return "知識庫資料夾目前沒有可讀取的文字文件。", []

    query_terms = _terms(query)
    if not query_terms:
        return "", []
    ranked = []
    for item in files:
        for chunk in item["chunks"]:
            ranked.append((_score(query_terms, item["name"], chunk), item, chunk))
    ranked.sort(key=lambda row: row[0], reverse=True)
    selected = []
    seen = set()
    for score, item, chunk in ranked:
        if item["id"] in seen:
            continue
        # Require more than a single incidental token overlap before attaching
        # a document; otherwise an unrelated old topic can steer the answer.
        if query_terms and score < 2:
            continue
        selected.append((score, item, chunk))
        seen.add(item["id"])
        if len(selected) >= 4:
            break
    if not selected and query_terms:
        scanned_pdfs = [
            item for item in files
            if item["is_pdf"] and not item["has_text"]
            and _score(query_terms, item["name"], "") >= 3
        ]
        if len(scanned_pdfs) == 1:
            item = scanned_pdfs[0]
            selected = [(1, item, "這份 PDF 沒有可擷取文字，將由 Gemini 直接辨識頁面內容。")]
    if not selected:
        return "", []

    context_parts = [
        f"來源：{item['name']}\n{chunk[:3500]}"
        for _, item, chunk in selected
    ]
    context = "\n\n".join(context_parts)[:MAX_CONTEXT_CHARS]

    pdfs = []
    for _, item, _ in selected:
        if not item["is_pdf"]:
            continue
        try:
            raw = _read_file(item["id"], _access_token(), item["mime_type"])
            if len(raw) <= MAX_PDF_INLINE_BYTES:
                pdfs.append({
                    "name": item["name"],
                    "mime_type": "application/pdf",
                    "data": base64.b64encode(raw).decode("ascii"),
                })
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
            pass
        break
    return context, pdfs
