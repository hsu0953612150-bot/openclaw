"""Per-answer playback buttons with private durable storage, independent of TTS quotas."""
import base64
import hashlib
import os
import re
import secrets
import threading
import time

from cryptography.fernet import Fernet, InvalidToken
from codex_line_bridge import BridgeStore

TAB = "語音播放"
TTL = 7 * 24 * 60 * 60
_lock = threading.Lock()


def cipher():
    secret = os.getenv("GOOGLE_OAUTH_REFRESH_TOKEN", "")
    if not secret:
        raise RuntimeError("Playback requires the existing private Google authorization")
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(("daga-playback-v1:" + secret).encode()).digest()))


class PlaybackStore:
    def __init__(self, store=None):
        self.store = store or BridgeStore()

    def save(self, text):
        token = secrets.token_urlsafe(24)
        digest = hashlib.sha256(token.encode()).hexdigest()
        spoken = text if len(text) <= 6000 else text[:6000] + "\n這則回覆較長，語音讀到這裡；完整內容請看LINE文字。"
        encrypted = cipher().encrypt(spoken.encode()).decode()
        with _lock:
            try:
                self.store.read(TAB, "A1:C1")
            except Exception as error:
                if getattr(error, "code", None) != 400:
                    raise
                self.store.cloud.api(self.store.base + ":batchUpdate", {"requests": [{"addSheet": {"properties": {"title": TAB, "gridProperties": {"rowCount": 1000, "columnCount": 10}}}}]}, method="POST")
                self.store.write(TAB, "A1:C1", [["連結雜湊", "加密回覆", "到期時間"]])
            self.store.append(TAB, [digest, encrypted, str(int(time.time()) + TTL)])
        return token

    def load(self, token):
        if not re.fullmatch(r"[A-Za-z0-9_-]{32}", token):
            raise LookupError("播放連結不存在或已到期")
        digest = hashlib.sha256(token.encode()).hexdigest()
        for row in self.store.read(TAB, "A2:C"):
            if len(row) >= 3 and secrets.compare_digest(row[0], digest):
                if float(row[2]) <= time.time():
                    break
                try:
                    return cipher().decrypt(row[1].encode(), ttl=TTL).decode()
                except InvalidToken:
                    break
        raise LookupError("播放連結不存在或已到期")


def playback_card(text, base_url, store=None):
    token = (store or PlaybackStore()).save(text)
    return {"type": "flex", "altText": "播放這則回覆的語音（7天內有效）", "contents": {
        "type": "bubble", "size": "kilo", "body": {"type": "box", "layout": "vertical", "contents": [
            {"type": "button", "style": "primary", "color": "#16754A", "action": {"type": "uri", "label": "▶ 播放語音", "uri": base_url.rstrip("/") + "/listen/" + token}},
            {"type": "text", "text": "這則回覆專屬・7天內可重播", "size": "xs", "color": "#666666", "align": "center", "margin": "sm"}
        ]}}}


def final_messages(text, base_url, store=None, audio_message=None):
    # 2,000 Unicode characters stay below LINE's UTF-16 length limit, including emoji.
    messages = [{"type": "text", "text": text[i:i+2000]} for i in range(0, len(text), 2000)] or [{"type": "text", "text": "已完成，沒有文字輸出。"}]
    messages.append(playback_card(text, base_url, store))
    if audio_message:
        try:
            messages.append(audio_message(text))
        except Exception:
            pass  # The durable button remains available even when synthesis fails.
    return messages
