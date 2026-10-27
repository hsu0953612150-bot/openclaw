"""Cloud is authoritative. Incremental local copies and search never execute content."""
import argparse
import datetime as dt
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import threading
import time
import urllib.parse
import urllib.request

from video_knowledge import Cloud, load_local_env

ROOT = Path(__file__).resolve().parent.parent
BACKUP = ROOT / "data" / "cloud-backup"
MAX_FILE = 128 * 1024 * 1024
MAX_CYCLE = 512 * 1024 * 1024
NATIVE = {
    "application/vnd.google-apps.document": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".docx"),
    "application/vnd.google-apps.spreadsheet": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"),
    "application/vnd.google-apps.presentation": ("application/vnd.openxmlformats-officedocument.presentationml.presentation", ".pptx"),
    "application/vnd.google-apps.drawing": ("application/pdf", ".pdf"),
}
_backup_lock = threading.Lock()


def stamp():
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(timespec="seconds")


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix(path.suffix + ".pending")
    pending.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    pending.replace(path)


def read_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def contained(root, relative):
    root = Path(root).resolve()
    target = (root / relative).resolve()
    if not target.is_relative_to(root):
        raise ValueError("Backup path escaped the selected folder")
    return target


def digest(path, algorithm="sha256"):
    value = hashlib.new(algorithm)
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def drive_files(cloud):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", cloud.folder_id or ""):
        raise ValueError("Knowledge folder is not configured")
    pending, seen, result = [cloud.folder_id], set(), []
    while pending:
        folder = pending.pop(0)
        if folder in seen:
            continue
        seen.add(folder)
        page = ""
        while True:
            params = {"q": "'" + folder + "' in parents and trashed=false", "pageSize": 100, "fields": "nextPageToken,files(id,name,mimeType,size,md5Checksum,modifiedTime,version,appProperties)", "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}
            if page:
                params["pageToken"] = page
            data = cloud.api("https://www.googleapis.com/drive/v3/files?" + urllib.parse.urlencode(params))
            for item in data.get("files", []):
                if not re.fullmatch(r"[A-Za-z0-9_-]+", item.get("id", "")):
                    raise ValueError("Invalid Drive file identifier")
                if item.get("mimeType") == "application/vnd.google-apps.folder":
                    pending.append(item["id"])
                elif item.get("appProperties", {}).get("daga_role") == "lookup-index":
                    continue  # Do not recursively index the generated search index itself.
                else:
                    result.append(item)
            page = data.get("nextPageToken", "")
            if len(result) > 5000:
                raise RuntimeError("More than 5000 files; backup needs a larger configured batch")
            if not page:
                break
    return result


def download(cloud, item, destination):
    mime = item.get("mimeType", "")
    encoded = urllib.parse.quote(item["id"], safe="")
    if mime.startswith("application/vnd.google-apps."):
        if mime not in NATIVE:
            raise RuntimeError("This Google-native format cannot be exported automatically")
        url = "https://www.googleapis.com/drive/v3/files/" + encoded + "/export?" + urllib.parse.urlencode({"mimeType": NATIVE[mime][0]})
    else:
        url = "https://www.googleapis.com/drive/v3/files/" + encoded + "?alt=media&supportsAllDrives=true"
    request = urllib.request.Request(url, headers={"Authorization": "Bearer " + cloud.token()})
    total = 0
    with urllib.request.urlopen(request, timeout=60) as response, destination.open("wb") as output:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_FILE:
                raise RuntimeError("File exceeds the 128MB automatic backup limit")
            output.write(chunk)
    if item.get("md5Checksum") and digest(destination, "md5") != item["md5Checksum"]:
        raise RuntimeError("Downloaded file checksum did not match Drive")
    return total


def snapshot_sheets(cloud):
    base = "https://sheets.googleapis.com/v4/spreadsheets/" + cloud.sheet_id
    meta = cloud.api(base + "?fields=properties(title),sheets(properties(title))")
    titles = [s["properties"]["title"] for s in meta.get("sheets", [])]
    params = [("ranges", "'" + name.replace("'", "''") + "'") for name in titles]
    data = cloud.api(base + "/values:batchGet?" + urllib.parse.urlencode(params))
    return {"captured_at": stamp(), "spreadsheet_id": cloud.sheet_id, "title": meta.get("properties", {}).get("title", ""), "tabs": {title: rows.get("values", []) for title, rows in zip(titles, data.get("valueRanges", []))}}


def sync_once(cloud=None, root=None, fetch_file=None):
    root = Path(root or BACKUP)
    root.mkdir(parents=True, exist_ok=True)
    cloud = cloud or Cloud()
    fetch_file = fetch_file or download
    with _backup_lock:
        previous = read_json(root / "manifest.json", {"files": {}})
        current = {"started_at": stamp(), "folder_id": cloud.folder_id, "files": dict(previous.get("files", {})), "errors": [], "downloaded": 0, "unchanged": 0, "bytes_downloaded": 0}
        items = drive_files(cloud)
        current["active_ids"] = [item["id"] for item in items]
        for item in items:
            key = item["id"]
            suffix = NATIVE.get(item.get("mimeType"), ("", ""))[1]
            name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", item.get("name", "file")).strip(" .")[:140] or "file"
            name += suffix if suffix and not name.lower().endswith(suffix) else ""
            relative = "files/" + key + "/" + name
            target = contained(root, relative)
            old = previous.get("files", {}).get(key, {})
            version = str(item.get("version") or item.get("modifiedTime") or item.get("md5Checksum") or "")
            try:
                if old.get("version") == version and old.get("path") == relative and target.is_file() and digest(target) == old.get("sha256"):
                    current["unchanged"] += 1
                    continue
                size = int(item.get("size") or 0)
                if size > MAX_FILE or current["bytes_downloaded"] + size > MAX_CYCLE:
                    raise RuntimeError("Backup batch size limit reached; retained for a later cycle")
                if shutil.disk_usage(root).free < max(size * 2, 1024 * 1024 * 1024):
                    raise RuntimeError("Not enough reserved disk space")
                target.parent.mkdir(parents=True, exist_ok=True)
                partial = target.with_name(target.name + ".partial")
                count = fetch_file(cloud, item, partial)
                if item.get("md5Checksum") and digest(partial, "md5") != item["md5Checksum"]:
                    raise RuntimeError("Downloaded file checksum did not match Drive")
                checksum = digest(partial)
                if target.exists():
                    old_hash = digest(target)
                    version_copy = contained(root, "versions/" + key + "/" + old_hash + "/" + target.name)
                    version_copy.parent.mkdir(parents=True, exist_ok=True)
                    if not version_copy.exists():
                        shutil.copy2(target, version_copy)
                partial.replace(target)
                current["files"][key] = dict(item, path=relative, version=version, sha256=checksum, downloaded_at=stamp())
                current["downloaded"] += 1
                current["bytes_downloaded"] += count
            except Exception as error:
                current["errors"].append({"id": key, "name": name, "error_type": type(error).__name__})
        try:
            snapshot = snapshot_sheets(cloud)
            write_json(root / "sheets" / (dt.datetime.now().strftime("%Y-%m-%d") + ".json"), snapshot)
            write_json(root / "sheets" / "latest.json", snapshot)
            current["sheet_tabs"] = len(snapshot["tabs"])
        except Exception as error:
            current["errors"].append({"source": "Sheets", "error_type": type(error).__name__})
        current["finished_at"] = stamp()
        current["complete"] = not current["errors"]
        write_json(root / "manifest.json", current)
        index = build_index(root)
        try:
            documents = sorted(index["documents"], key=lambda item: item["id"])
            serialized = json.dumps({"documents": documents}, ensure_ascii=False, sort_keys=True).encode()
            index_hash = hashlib.sha256(serialized).hexdigest()
            if index_hash != previous.get("published_index_sha256"):
                files = cloud.list_files("'" + cloud.folder_id + "' in parents and trashed=false and appProperties has { key='daga_role' and value='lookup-index' }", limit=1)
                cloud.upload("大G檢索索引.json", serialized, "application/json", {"daga_role": "lookup-index"}, files[0]["id"] if files else None)
            current["published_index_sha256"] = index_hash
        except Exception as error:
            current["errors"].append({"source": "cloud_search_index", "error_type": type(error).__name__})
        current["complete"] = not current["errors"]
        write_json(root / "manifest.json", current)
        return {k: current[k] for k in ("complete", "downloaded", "unchanged", "bytes_downloaded", "errors", "finished_at")}


def build_index(root):
    from drive_knowledge import _extract_text, _split_text
    manifest = read_json(root / "manifest.json", {})
    documents = []
    for key, item in manifest.get("files", {}).items():
        try:
            if key not in manifest.get("active_ids", manifest.get("files", {})):
                continue
            path = contained(root, item["path"])
            if re.search(r"(?:^\.env|credential|oauth|private.?key|secret|\.pem$|\.key$|\.pfx$)", path.name, re.I):
                continue
            if not path.is_file() or path.stat().st_size > 12 * 1024 * 1024:
                continue
            if digest(path) != item.get("sha256"):
                continue  # Do not publish an altered local copy back into cloud search.
            text = _extract_text(path.name, item.get("mimeType", ""), path.read_bytes())
            for name, value in os.environ.items():
                if any(part in name for part in ("TOKEN", "SECRET", "API_KEY", "ACCOUNT_JSON")) and len(value) >= 12:
                    text = text.replace(value, "[憑證已遮蔽]")
            text = re.sub(r"-----BEGIN (?:RSA )?PRIVATE KEY-----[\s\S]*?-----END (?:RSA )?PRIVATE KEY-----|\bsk-[A-Za-z0-9_-]{16,}", "[憑證已遮蔽]", text)
            if text.strip():
                documents.append({"id": key, "name": item.get("name", path.name), "path": item["path"], "source_url": "https://drive.google.com/file/d/" + key + "/view", "chunks": _split_text(text[:50000]), "modified_time": item.get("modifiedTime", "")})
        except Exception:
            continue
    index = {"built_at": stamp(), "cloud_sync_at": manifest.get("finished_at"), "documents": documents}
    write_json(root / "index.json", index)
    return index


def search_local(query, root=None, owner=""):
    from drive_knowledge import _terms, _score, _split_text
    root = Path(root or BACKUP)
    terms = _terms(query)
    if not terms:
        return {}
    index = read_json(root / "index.json", {})
    documents = list(index.get("documents", []))
    snapshot = read_json(root / "sheets/latest.json", {})
    history, profile = [], {}
    for row in snapshot.get("tabs", {}).get("UserMemory", []):
        if row and row[0] == owner and len(row) > 2:
            try:
                history = json.loads(row[2])[-8:]
                profile = json.loads(row[4]) if len(row) > 4 and row[4] else {}
                text = json.dumps({"對話": history, "個人記錄": profile}, ensure_ascii=False)
                documents.append({"name": "D槽對話備份", "chunks": _split_text(text), "source_url": "Google Sheets UserMemory"})
            except ValueError:
                pass
    rows = snapshot.get("tabs", {}).get("Liveboard Memory", [])
    if rows and "LINE 使用者 ID" in rows[0]:
        column = rows[0].index("LINE 使用者 ID")
        for row in rows[1:]:
            if len(row) > column and row[column] == owner:
                text = "；".join(value for i, value in enumerate(row) if i != column)
                documents.append({"name": "D槽記憶備份", "chunks": [text], "source_url": "Google Sheets Liveboard Memory"})
    ranked = []
    for document in documents:
        for chunk in document.get("chunks", []):
            score = _score(terms, document.get("name", ""), chunk)
            if score >= 2:
                ranked.append((score, document, chunk))
    ranked.sort(key=lambda value: value[0], reverse=True)
    selected, seen = [], set()
    for _, document, chunk in ranked:
        identity = document.get("source_url", "") + chunk[:50]
        if identity in seen:
            continue
        seen.add(identity)
        selected.append("來源：D槽備份／" + document.get("name", "") + "\n雲端來源：" + document.get("source_url", "") + "\n" + chunk[:2500])
        if len(selected) >= 4:
            break
    if not selected:
        return {}
    return {"text": ("[本機優先檢索；備份時間：" + str(index.get("cloud_sync_at", snapshot.get("captured_at", "未知"))) + "；舊紀錄不能當作即時資訊]\n" + "\n\n".join(selected))[:11000], "history": history, "profile": profile, "source": "local", "backup_at": index.get("cloud_sync_at", "")}


def start_background_backup():
    def run():
        delay = 0
        while True:
            if delay:
                time.sleep(delay)
            try:
                report = sync_once()
                logging.info("Cloud backup: downloaded=%s unchanged=%s errors=%s", report["downloaded"], report["unchanged"], len(report["errors"]))
                delay = 600
            except Exception as error:
                logging.warning("Cloud backup deferred: %s", type(error).__name__)
                delay = 300
    thread = threading.Thread(target=run, name="daga-cloud-backup", daemon=True)
    thread.start()
    return thread


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--search", default="")
    args = parser.parse_args()
    load_local_env(ROOT / ".env")
    if args.search:
        state = read_json(ROOT / "data/codex-bridge/state.json", {})
        print(json.dumps(search_local(args.search, owner=state.get("owner_id", "")), ensure_ascii=False))
    else:
        print(json.dumps(sync_once(), ensure_ascii=False))

