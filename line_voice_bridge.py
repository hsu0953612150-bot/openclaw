"""Route the paired owner's LINE voice through the same PC queue as typed text."""
import base64
import hashlib
import re

from codex_line_bridge import BridgeStore, route_text
from openai_fallback import transcribe_audio_fallback
from message_routing import route_classified_text

MAX_COMMAND_AUDIO_BYTES = 12 * 1024 * 1024
MAX_COMMAND_AUDIO_DURATION_MS = 2 * 60 * 1000


def route_audio_to_codex(user_id, message_id, load_audio, source_type="user", duration_ms=0, store=None, transcribe=None, normal_chat=None):
    if source_type != "user":
        return None
    store = store or BridgeStore()
    config = store.control()
    if config.get("enabled") != "true" or not config.get("owner_id") or config["owner_id"] != user_id:
        return None
    voice_event_id = "audio:" + message_id
    job_id = hashlib.sha256((user_id + ":" + voice_event_id).encode()).hexdigest()[:16]
    if any(row[0] == job_id for _, row in store.tasks()):
        return "這段語音已收到，不會重複執行。任務編號：" + job_id
    if duration_ms > MAX_COMMAND_AUDIO_DURATION_MS:
        return "這段語音超過2分鐘，尚未交給電腦執行；請用較短的語音說明指令。"
    audio = load_audio()
    if not isinstance(audio, (bytes, bytearray)) or not 0 < len(audio) <= MAX_COMMAND_AUDIO_BYTES:
        return "語音檔為空或超過12MB，尚未交給電腦執行；請重傳較短的語音。"
    transcript = (transcribe or transcribe_audio_fallback)(base64.b64encode(audio).decode("ascii"), "audio/m4a")
    if not isinstance(transcript, str) or not transcript.strip():
        return "這段語音沒有辨識出文字，尚未執行；請重新說一次。"
    transcript = transcript.strip()
    if re.match(r"^(?:確認|确认|同意|拒絕|拒绝|回答)\s+[A-Fa-f0-9]{8}\b", transcript):
        return "這段語音像是在回答操作核准。請依LINE提示用文字回覆確認碼，這次沒有授權或執行。"
    control = re.fullmatch(r"(電腦狀態|电脑状态|停止任務|停止任务)[。.!！?？]*", transcript)
    if control:
        transcript = control[1]
    latest = store.control()
    if latest.get("enabled") != "true" or latest.get("owner_id") != user_id:
        return "語音已轉錄，但本人綁定狀態已變更，沒有交給電腦執行。"
    acknowledgement = route_classified_text(user_id, transcript, voice_event_id, source_type, store)
    if acknowledgement is None:
        if normal_chat:
            normal_chat(transcript)
            return ""
        return None
    return "語音辨識：" + transcript[:600] + "\n\n" + acknowledgement
