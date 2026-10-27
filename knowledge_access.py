"""Owner-only outbound local lookup; cloud fallback never requires a public PC port."""
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time
import uuid

from codex_line_bridge import BridgeStore

TAB = "大G本機查詢"
HEADERS = ["查詢ID", "建立時間", "本人ID", "查詢文字", "狀態", "到期時間", "結果", "更新時間"]
_prepare_lock = threading.Lock()
_cache = {}


def prepare_lookup(store):
    with _prepare_lock:
        try:
            store.read(TAB, "A1:H1")
        except Exception as error:
            if getattr(error, "code", None) != 400:
                raise
            store.cloud.api(store.base + ":batchUpdate", {"requests": [{"addSheet": {"properties": {"title": TAB, "gridProperties": {"rowCount": 2000, "columnCount": 10}}}}]}, method="POST")
            store.write(TAB, "A1:H1", [HEADERS])


def retrieve_local_context(user_id, query, store=None, wait_seconds=20):
    if not query or len(query.strip()) < 2:
        return {}
    if os.name == "nt":
        from cloud_backup import ROOT, read_json, search_local
        owner = read_json(ROOT / "data/codex-bridge/state.json", {}).get("owner_id", "")
        if user_id in ("codex-local", owner) and owner:
            return search_local(query, owner=owner)
        return {}
    store = store or BridgeStore()
    config = store.control()
    if config.get("owner_id") != user_id or config.get("enabled") != "true":
        return {}
    if time.time() - float(config.get("worker_heartbeat") or 0) >= 150 or config.get("worker_status") in ("處理中", "等待確認", "已暫停"):
        return {}
    key = (user_id, hashlib.sha256(query.encode()).hexdigest())
    cached = _cache.get(key)
    if cached and cached[0] > time.time():
        return cached[1]
    prepare_lookup(store)
    request_id = uuid.uuid4().hex
    created = time.time()
    result = store.append(TAB, [request_id, str(created), user_id, query[:1000], "queued", str(created + wait_seconds), "", ""])
    updated_range = result.get("updates", {}).get("updatedRange", "") if isinstance(result, dict) else ""
    row_match = re.search(r"!A(\d+)", updated_range)
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        time.sleep(min(4, max(0, deadline - time.monotonic())))
        cells = f"A{row_match[1]}:H{row_match[1]}" if row_match else "A2:H"
        rows = store.read(TAB, cells)
        for row in rows:
            if len(row) >= 7 and row[0] == request_id and row[2] == user_id and row[4] == "done":
                context = json.loads(row[6] or "{}")
                if not isinstance(context, dict):
                    return {}
                _cache[key] = (time.time() + 20, context)
                if len(_cache) > 100:
                    _cache.clear()
                return context
    return {}


def process_local_lookups(store, owner):
    from cloud_backup import search_local
    try:
        rows = store.read(TAB, "A2:H")
    except Exception as error:
        if getattr(error, "code", None) == 400:
            prepare_lookup(store)
            return
        raise
    processed = 0
    for index, raw in enumerate(rows, 2):
        row = list(raw) + [""] * (8 - len(raw))
        if row[4] != "queued":
            continue
        try:
            expired = time.time() > float(row[5] or 0)
        except (TypeError, ValueError):
            expired = True
        if not owner or row[2] != owner or expired:
            row[4], row[7] = "expired", str(time.time())
        else:
            context = search_local(row[3][:1000], owner=owner)
            # Only data from this fixed backup root is returned; no arbitrary path or command.
            encoded = json.dumps(context, ensure_ascii=False)
            if len(encoded) > 20000:
                context = {"text": context.get("text", "")[:8000], "source": "local", "backup_at": context.get("backup_at", "")}
                encoded = json.dumps(context, ensure_ascii=False)
            row[4], row[6], row[7] = "done", encoded, str(time.time())
        store.write(TAB, f"A{index}:H{index}", [row[:8]])
        processed += 1
        if processed >= 20:
            break


def retrieve_cloud_project_context(user_id, query, store=None):
    from drive_knowledge import _terms, _score
    store = store or BridgeStore()
    owner = store.control().get("owner_id", "")
    if not owner or user_id not in (owner, "codex-local"):
        return ""
    cloud = store.cloud
    if not re.fullmatch(r"[A-Za-z0-9_-]+", cloud.folder_id or ""):
        return ""
    files = cloud.list_files("'" + cloud.folder_id + "' in parents and trashed=false and appProperties has { key='daga_role' and value='lookup-index' }", limit=1)
    if not files:
        return ""
    index = cloud.api("https://www.googleapis.com/drive/v3/files/" + files[0]["id"] + "?alt=media")
    terms = _terms(query)
    ranked = []
    for item in index.get("documents", []):
        for chunk in item.get("chunks", []):
            score = _score(terms, item.get("name", ""), chunk)
            if score >= 2:
                ranked.append((score, item, chunk))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return "\n\n".join("來源：雲端／" + item.get("name", "") + "\n" + item.get("source_url", "") + "\n" + chunk[:2500] for _, item, chunk in ranked[:4])[:11000]

