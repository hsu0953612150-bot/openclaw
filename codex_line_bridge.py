"""Owner-paired LINE to local Codex transport, durable in the existing private Sheet."""
import hashlib
import hmac
import json
import re
import threading
import time
import urllib.parse
import urllib.error
import urllib.request
import uuid

from video_knowledge import Cloud

CONTROL = "Codex連線"
TASKS = "Codex任務"
QUESTIONS = "Codex確認"
CONTROL_KEYS = ("owner_id", "pairing_sha256", "pairing_expires", "worker_heartbeat", "worker_status", "active_job", "enabled", "pairing_attempts", "thread_id", "cancel_job")
TASK_HEADERS = ["任務ID", "建立時間", "使用者ID", "訊息ID", "指令", "狀態", "結果", "Codex對話ID", "更新時間", "已回覆"]
QUESTION_HEADERS = ["確認碼", "任務ID", "使用者ID", "說明", "狀態", "回答", "建立時間", "類型"]
_lock = threading.RLock()


def notification_signature(secret, body):
    key = hmac.new(secret.encode(), b"daga-codex-delivery-v1", hashlib.sha256).digest()
    return hmac.new(key, body, hashlib.sha256).hexdigest()


def verify_notification(secret, body, signature):
    if not secret or len(body) > 1024 or not hmac.compare_digest(notification_signature(secret, body), signature):
        raise PermissionError("通知驗證失敗")
    data = json.loads(body)
    if abs(time.time() - float(data.get("timestamp", 0))) > 300:
        raise PermissionError("通知已逾時")
    if data.get("kind") not in ("result", "question", "probe"):
        raise ValueError("通知類型無效")
    return data


def line_api(path, token, body=None, delivery_id=""):
    headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
    if delivery_id:
        headers["X-Line-Retry-Key"] = str(uuid.uuid5(uuid.NAMESPACE_URL, "daga-codex:" + delivery_id))
    request = urllib.request.Request("https://api.line.me/v2/bot/" + path, data=json.dumps(body, ensure_ascii=False).encode() if body is not None else None, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
        return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as error:
        if error.code == 409 and delivery_id:
            return {}
        raise


def deliver_notification(data, token, store=None):
    """Signed notifications deliver only a stored owner's result, never caller text."""
    store = store or BridgeStore()
    config = store.control()
    if data["kind"] == "probe":
        info = line_api("info", token)
        return {"ok": True, "paired": bool(config.get("owner_id")), "line_bot_name": info.get("displayName")}
    owner = config.get("owner_id", "")
    if not owner or config.get("enabled") != "true":
        raise PermissionError("電腦尚未完成本人綁定")
    with _lock:
        if data["kind"] == "result":
            match = next(((index, row) for index, row in store.tasks() if row[0] == data.get("id") and row[2] == owner), None)
            if not match or match[1][5] not in ("completed", "failed"):
                raise ValueError("找不到已完成的本人任務")
            index, row = match
            if row[9]:
                return {"ok": True, "already_delivered": True}
            text = row[6] + "\n\n任務：" + row[0]
        else:
            match = next(((index, row) for index, row in store.questions() if row[0] == data.get("id") and row[2] == owner and row[4] == "pending"), None)
            if not match or time.time() - float(match[1][6]) > 1800:
                raise ValueError("找不到有效的本人確認")
            row = match[1]
            instruction = f"回答 {row[0]} 你的答案" if row[7] == "question" else f"確認 {row[0]}」或「拒絕 {row[0]}"
            text = f"Codex需要你的{'回答' if row[7] == 'question' else '確認'}：\n{row[3]}\n\n請回覆「{instruction}」。30分鐘內未回答就不授權。"
        chunks = [text[i:i + 4000] for i in range(0, min(len(text), 20000), 4000)] or ["已完成，沒有文字輸出。"]
        line_api("message/push", token, {"to": owner, "messages": [{"type": "text", "text": part} for part in chunks]}, data["kind"] + ":" + data["id"])
        if data["kind"] == "result":
            row[9] = timestamp()
            store.task_update(index, row)
        return {"ok": True}


def timestamp():
    return str(int(time.time()))


class BridgeStore:
    def __init__(self, cloud=None):
        self.cloud = cloud or Cloud()
        self.base = "https://sheets.googleapis.com/v4/spreadsheets/" + self.cloud.sheet_id

    def read(self, tab, cells="A:J"):
        target = urllib.parse.quote("'" + tab + "'!" + cells, safe="")
        return self.cloud.api(self.base + "/values/" + target).get("values", [])

    def write(self, tab, cells, values):
        target = urllib.parse.quote("'" + tab + "'!" + cells, safe="")
        return self.cloud.api(self.base + "/values/" + target + "?valueInputOption=RAW", {"values": values}, method="PUT")

    def append(self, tab, values):
        target = urllib.parse.quote("'" + tab + "'!A:J", safe="")
        return self.cloud.api(self.base + "/values/" + target + ":append?valueInputOption=RAW&insertDataOption=INSERT_ROWS", {"values": [values]}, method="POST")

    def prepare(self):
        meta = self.cloud.api(self.base + "?fields=sheets(properties(title))")
        existing = {s["properties"]["title"] for s in meta.get("sheets", [])}
        for tab, headers in ((CONTROL, ["設定", "值"]), (TASKS, TASK_HEADERS), (QUESTIONS, QUESTION_HEADERS)):
            if tab not in existing:
                self.cloud.api(self.base + ":batchUpdate", {"requests": [{"addSheet": {"properties": {"title": tab, "gridProperties": {"rowCount": 2000, "columnCount": 10}}}}]}, method="POST")
                self.write(tab, "A1", [headers])
                if tab == CONTROL:
                    self.write(tab, "A2", [[key, ""] for key in CONTROL_KEYS])

    def control(self):
        try:
            return {row[0]: row[1] if len(row) > 1 else "" for row in self.read(CONTROL, "A2:B11") if row}
        except Exception as error:
            # An unprovisioned bridge leaves the original LINE assistant available.
            if getattr(error, "code", None) == 400:
                return {}
            raise

    def set_control(self, key, value):
        index = CONTROL_KEYS.index(key) + 2
        self.write(CONTROL, f"B{index}", [[str(value)]])

    def tasks(self):
        return [(index, row + [""] * (10 - len(row))) for index, row in enumerate(self.read(TASKS, "A2:J"), 2) if row]

    def task_update(self, index, row):
        self.write(TASKS, f"A{index}:J{index}", [row[:10]])

    def questions(self):
        return [(index, row + [""] * (8 - len(row))) for index, row in enumerate(self.read(QUESTIONS, "A2:H"), 2) if row]

    def question_update(self, index, row):
        self.write(QUESTIONS, f"A{index}:H{index}", [row[:8]])


def route_text(user_id, text, message_id, source_type="user", store=None):
    """Return an acknowledgement when consumed, otherwise None for normal chat."""
    if source_type != "user":
        return None
    store = store or BridgeStore()
    with _lock:
        config = store.control()
        if not config or config.get("enabled") != "true":
            return None
        owner = config.get("owner_id", "")
        pair = re.fullmatch(r"(?:綁定電腦|绑定电脑)\s+([A-Za-z0-9]{12})", text.strip())
        if pair:
            if owner:
                return "電腦已綁定，請由電腦端重新產生綁定碼。"
            attempts = int(config.get("pairing_attempts") or 0)
            if attempts >= 20 or float(config.get("pairing_expires") or 0) < time.time():
                return "綁定碼已失效，請在電腦端重新產生。"
            supplied = hashlib.sha256(pair[1].upper().encode()).hexdigest()
            if not hmac.compare_digest(supplied, config.get("pairing_sha256", "")):
                store.set_control("pairing_attempts", attempts + 1)
                return "綁定碼不正確。"
            store.set_control("owner_id", user_id)
            store.set_control("pairing_sha256", "")
            return "已綁定這台電腦。你接下來傳的所有文字會交給本機 Codex；回覆會送回這個 LINE 對話。可傳「電腦狀態」查看連線。"
        if not owner or user_id != owner:
            return None
        if text.strip() in ("電腦狀態", "电脑状态"):
            age = time.time() - float(config.get("worker_heartbeat") or 0)
            state = config.get("worker_status") or "等待連線"
            return ("電腦連線正常" if age < 150 else "電腦目前離線，訊息會保留待處理") + "；狀態：" + state + ("；任務：" + config["active_job"] if config.get("active_job") else "")
        if text.strip() in ("停止任務", "停止任务"):
            if not config.get("active_job"):
                return "目前沒有回報正在執行的任務。"
            store.set_control("cancel_job", config["active_job"])
            return "已通知電腦停止目前任務；已完成的操作不會自動還原。"
        response = re.fullmatch(r"(確認|确认|拒絕|拒绝|回答)\s+([A-Fa-f0-9]{8})(?:\s+([\s\S]+))?", text.strip())
        if response:
            for index, row in store.questions():
                if row[0] == response[2].upper() and row[2] == owner and row[4] == "pending":
                    if time.time() - float(row[6]) > 1800:
                        return "這個確認已逾時，沒有授權執行。"
                    if row[7] == "question" and response[1] != "回答":
                        return "這是問題，請用「回答 確認碼 你的答案」。"
                    row[4] = "answered"
                    row[5] = response[3] if response[1] == "回答" else ("accept" if response[1] in ("確認", "确认") else "decline")
                    store.question_update(index, row)
                    return "收到，已將你的決定交回電腦。"
            return "找不到仍在等待的確認，沒有執行授權。"
        if len(text) > 12000:
            return "文字超過12000字，請分段傳送。"
        job_id = hashlib.sha256((user_id + ":" + message_id).encode()).hexdigest()[:16]
        tasks = store.tasks()
        if any(row[0] == job_id for _, row in tasks):
            return "這則訊息已收到，任務編號：" + job_id
        if sum(row[2] == owner and row[5] in ("queued", "running") for _, row in tasks) >= 20:
            return "已有20筆待處理任務，請等目前任務完成後再傳。"
        store.append(TASKS, [job_id, timestamp(), owner, message_id, text, "queued", "", "", timestamp(), ""])
        online = time.time() - float(config.get("worker_heartbeat") or 0) < 150
        return ("已交給電腦 Codex 排隊處理" if online else "電腦目前離線，已保留任務，連線後處理") + "。任務編號：" + job_id
