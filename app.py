import base64
import datetime
import io
import json
import os
import secrets
import subprocess
import threading
import time
import urllib.error
import urllib.request
import wave

import gspread
from flask import Flask, abort, render_template, request
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    MessagingApi,
    MessagingApiBlob,
    PushMessageRequest,
    ReplyMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import AudioMessageContent, ImageMessageContent, MessageEvent, TextMessageContent
from oauth2client.service_account import ServiceAccountCredentials


app = Flask(__name__)
configuration = Configuration(access_token=os.getenv("LINE_CHANNEL_ACCESS_TOKEN"))
handler = WebhookHandler(os.getenv("LINE_CHANNEL_SECRET"))

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
SPREADSHEET_RAW_ID = os.getenv("SPREADSHEET_ID", "")
SPREADSHEET_ID = (
    SPREADSHEET_RAW_ID.split("/d/")[1].split("/")[0]
    if "/d/" in SPREADSHEET_RAW_ID
    else SPREADSHEET_RAW_ID
)
SHEET_NAME = os.getenv("GOOGLE_SHEET_TAB", "UserMemory")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://openclaw-jc14.onrender.com").rstrip("/")
AUDIO_URL_TTL_SECONDS = 15 * 60
MAX_HISTORY_MESSAGES = max(2, int(os.getenv("MAX_HISTORY_MESSAGES", "8")))
GEMINI_LIVE_MODEL = os.getenv("GEMINI_LIVE_MODEL", "gemini-3.8-live")
LIVE_TOKEN_LIMIT_PER_HOUR = max(1, int(os.getenv("LIVE_TOKEN_LIMIT_PER_HOUR", "30")))

SYSTEM_INSTRUCTION = """你是 LINE 官方帳號「大G」的 AI 助理，使用繁體中文回答。
只回答使用者實際提出的問題。若使用者只說「測試訊息」、問候或沒有提出具體問題，簡短確認收到即可，不要猜測主題或補充無關建議。
回答清楚、親切、精簡；不要聲稱已保存資料或完成外部操作，除非系統已明確完成。"""

_sheet_lock = threading.Lock()
_audio_lock = threading.Lock()
_audio_cache = {}
_live_token_lock = threading.Lock()
_live_token_requests = []


@app.get("/")
def health_check():
    return "大G LINE/Gemini bridge is online.", 200


@app.get("/health")
def health():
    configured = bool(
        GEMINI_API_KEY
        and os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
        and os.getenv("LINE_CHANNEL_SECRET")
        and os.getenv("GCP_SERVICE_ACCOUNT_JSON")
        and SPREADSHEET_ID
    )
    return {"ok": configured, "service": "daga-line-gemini", "model": GEMINI_MODEL}, (200 if configured else 503)


@app.get("/voice")
def live_voice_page():
    return render_template("live_voice.html")


@app.post("/api/live-token")
def create_live_token():
    now = time.time()
    with _live_token_lock:
        _live_token_requests[:] = [timestamp for timestamp in _live_token_requests if now - timestamp < 3600]
        if len(_live_token_requests) >= LIVE_TOKEN_LIMIT_PER_HOUR:
            return {"error": "目前即時語音使用次數已達暫時上限，請稍後再試。"}, 429
        _live_token_requests.append(now)

    if not GEMINI_API_KEY:
        return {"error": "Gemini Live is not configured."}, 503

    now_utc = datetime.datetime.now(datetime.timezone.utc)
    live_model = f"models/{GEMINI_LIVE_MODEL.removeprefix('models/')}"
    token_body = json.dumps({
        "uses": 1,
        "expireTime": (now_utc + datetime.timedelta(minutes=30)).isoformat().replace("+00:00", "Z"),
        "newSessionExpireTime": (now_utc + datetime.timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
        "fieldMask": "model,generationConfig.responseModalities",
        "bidiGenerateContentSetup": {
            "model": live_model,
            "generationConfig": {"responseModalities": ["AUDIO"]},
        },
    }).encode("utf-8")
    token_request = urllib.request.Request(
        "https://generativelanguage.googleapis.com/v1beta/auth_tokens",
        data=token_body,
        headers={"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(token_request, timeout=20) as response:
            token_result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        try:
            provider_error = error.read().decode("utf-8", errors="replace")
        except Exception:
            provider_error = "<response body unavailable>"
        app.logger.error(
            "Could not create Gemini Live ephemeral token (HTTP %s): %s",
            error.code,
            provider_error[:2000],
        )
        return {"error": "Could not start a live voice session. Please try again later."}, 502
    except (urllib.error.URLError, TimeoutError):
        app.logger.exception("Could not reach Gemini Live token service")
        return {"error": "Could not reach Gemini Live. Please try again later."}, 502

    token = token_result.get("name")
    if not token:
        app.logger.error("Gemini Live token response did not include a token name")
        return {"error": "Gemini Live returned an invalid session token."}, 502
    return {"token": token, "model": live_model}, 200


@app.get("/audio/<token>.mp3")
def get_generated_audio(token):
    now = time.time()
    with _audio_lock:
        expired = [key for key, item in _audio_cache.items() if item[1] < now]
        for key in expired:
            _audio_cache.pop(key, None)
        item = _audio_cache.get(token)
    if not item:
        abort(404)
    return app.response_class(
        item[0],
        mimetype="audio/mpeg",
        headers={"Cache-Control": "public, max-age=300", "Content-Disposition": "inline"},
    )


@app.post("/callback")
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        app.logger.warning("Rejected LINE webhook with invalid signature")
        abort(400)
    return "OK", 200


def get_gspread_client():
    raw = os.getenv("GCP_SERVICE_ACCOUNT_JSON", "")
    if not raw:
        raise RuntimeError("GCP_SERVICE_ACCOUNT_JSON is not configured")
    normalized = raw.replace("\\\\n", "\n").replace("\\n", "\n").strip()
    info = json.loads(normalized)
    scopes = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    credentials = ServiceAccountCredentials.from_json_keyfile_dict(info, scopes)
    return gspread.authorize(credentials)


def get_sheet():
    if not SPREADSHEET_ID:
        raise RuntimeError("SPREADSHEET_ID is not configured")
    return get_gspread_client().open_by_key(SPREADSHEET_ID).worksheet(SHEET_NAME)


def load_history(user_id):
    with _sheet_lock:
        sheet = get_sheet()
        cell = sheet.find(user_id, in_column=1)
        if not cell:
            app.logger.info("Sheets history lookup: no existing row for LINE user")
            return []
        value = sheet.cell(cell.row, 3).value
        history = json.loads(value) if value else []
        if not isinstance(history, list):
            raise ValueError("Google Sheets conversation history is not a list")
        app.logger.info("Sheets history lookup succeeded")
        return history


def save_history(user_id, history):
    now = datetime.datetime.now(datetime.timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    payload = json.dumps(history[-MAX_HISTORY_MESSAGES:], ensure_ascii=False)
    with _sheet_lock:
        sheet = get_sheet()
        cell = sheet.find(user_id, in_column=1)
        if cell:
            sheet.update_cell(cell.row, 2, "chat_history")
            sheet.update_cell(cell.row, 3, payload)
            sheet.update_cell(cell.row, 4, now)
        else:
            sheet.append_row([user_id, "chat_history", payload, now], value_input_option="RAW")
    app.logger.info("Google Sheets write succeeded (conversation history updated)")


def gemini_generate(history, image_b64=None, audio_b64=None, audio_mime_type="audio/m4a"):
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not configured on the service")
    contents = []
    for index, message in enumerate(history[-MAX_HISTORY_MESSAGES:]):
        if audio_b64 and index == len(history[-MAX_HISTORY_MESSAGES:]) - 1 and message.get("content") == "[語音訊息]":
            continue
        role = "model" if message.get("role") == "assistant" else "user"
        content = message.get("content", "")
        contents.append({"role": role, "parts": [{"text": str(content)}]})
    if image_b64:
        contents.append({
            "role": "user",
            "parts": [
                {"text": "請根據使用者傳來的圖片內容回答。"},
                {"inline_data": {"mime_type": "image/jpeg", "data": image_b64}},
            ],
        })
    if audio_b64:
        contents.append({
            "role": "user",
            "parts": [
                {"text": "請聆聽這段 LINE 語音，理解使用者以繁體中文表達的內容並直接回覆。除非使用者要求逐字稿，否則不要只輸出逐字轉錄。"},
                {"inline_data": {"mime_type": audio_mime_type, "data": audio_b64}},
            ],
        })

    body = json.dumps({
        "systemInstruction": {"parts": [{"text": SYSTEM_INSTRUCTION}]},
        "contents": contents,
        # This bot primarily handles short LINE turns; keep reasoning latency low
        # so requests do not hit the webhook worker's response timeout.
        "generationConfig": {
            "maxOutputTokens": 800,
            "thinkingConfig": {"thinkingLevel": "low"},
        },
    }).encode("utf-8")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    request = urllib.request.Request(
        url,
        data=body,
        headers={"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:1000]
        raise RuntimeError(f"Gemini API returned HTTP {error.code}: {detail}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"Gemini API connection failed: {error.reason}") from error

    answer = "".join(
        part.get("text", "")
        for part in result.get("candidates", [{}])[0].get("content", {}).get("parts", [])
    ).strip()
    if not answer:
        raise RuntimeError("Gemini returned an empty response")
    app.logger.info("Gemini request succeeded (model=%s)", GEMINI_MODEL)
    return answer[:4900]


def send_text(user_id, text):
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).push_message(
            PushMessageRequest(to=user_id, messages=[TextMessage(text=text)])
        )


def synthesize_reply_audio(text):
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not configured on the service")
    speak_text = text[:1200]
    body = json.dumps({
        "model": os.getenv("GEMINI_TTS_MODEL", "gemini-3.8-flash-lite-tts"),
        "input": [{
            "type": "user_input",
            "content": [{
                "type": "text",
                "text": speak_text,
                "annotations": [{
                    "type": "speech_metadata",
                    "style": "Use clear, natural Traditional Chinese with a warm conversational tone.",
                }],
            }],
        }],
        "response_format": {"type": "audio"},
        "generation_config": {"speech_config": [{"voice": os.getenv("GEMINI_TTS_VOICE", "Kore")}]},
    }).encode("utf-8")
    request = urllib.request.Request(
        "https://generativelanguage.googleapis.com/v1beta/interactions",
        data=body,
        headers={"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"Gemini TTS returned HTTP {error.code}: {detail}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"Gemini TTS connection failed: {error.reason}") from error

    audio_data = next((
        part.get("data")
        for step in result.get("steps", [])
        if step.get("type") == "model_output"
        for part in step.get("content", [])
        if part.get("type") == "audio" and part.get("data")
    ), None)
    if not audio_data:
        raise RuntimeError("Gemini TTS returned no audio data")
    wav_bytes = base64.b64decode(audio_data)
    with wave.open(io.BytesIO(wav_bytes), "rb") as wav_file:
        duration_ms = max(1, int(wav_file.getnframes() * 1000 / wav_file.getframerate()))
    converted = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0", "-f", "mp3", "-codec:a", "libmp3lame", "-b:a", "96k", "pipe:1"],
        input=wav_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
        timeout=30,
    ).stdout
    if not converted:
        raise RuntimeError("Audio conversion produced an empty MP3")
    return converted, duration_ms


def send_voice_reply(user_id, text):
    audio_mp3, duration_ms = synthesize_reply_audio(text)
    token = secrets.token_urlsafe(24)
    expires_at = time.time() + AUDIO_URL_TTL_SECONDS
    with _audio_lock:
        _audio_cache[token] = (audio_mp3, expires_at, duration_ms)
    audio_url = f"{PUBLIC_BASE_URL}/audio/{token}.mp3"
    payload = json.dumps({
        "to": user_id,
        "messages": [
            {"type": "text", "text": text},
            {"type": "audio", "originalContentUrl": audio_url, "duration": duration_ms},
        ],
    }).encode("utf-8")
    request = urllib.request.Request(
        "https://api.line.me/v2/bot/message/push",
        data=payload,
        headers={"Authorization": f"Bearer {os.getenv('LINE_CHANNEL_ACCESS_TOKEN')}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            if response.status >= 300:
                raise RuntimeError(f"LINE audio reply returned HTTP {response.status}")
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"LINE audio reply returned HTTP {error.code}: {detail}") from error
    app.logger.info("LINE text and voice reply sent successfully")


def process_message(user_id, user_text=None, image_b64=None, audio_b64=None, audio_mime_type="audio/m4a"):
    try:
        try:
            history = load_history(user_id)
        except Exception:
            app.logger.exception("Google Sheets history read failed; continuing without prior context")
            history = []
        if user_text:
            history.append({"role": "user", "content": user_text})
        elif image_b64:
            history.append({"role": "user", "content": "[圖片]"})
        elif audio_b64:
            # Keep a marker in conversation context; do not persist a voice transcript.
            history.append({"role": "user", "content": "[語音訊息]"})
        answer = gemini_generate(
            history,
            image_b64=image_b64,
            audio_b64=audio_b64,
            audio_mime_type=audio_mime_type,
        )
        history.append({"role": "assistant", "content": answer})
        try:
            save_history(user_id, history)
        except Exception:
            app.logger.exception("Google Sheets write failed; AI reply will still be sent")
        if audio_b64:
            try:
                send_voice_reply(user_id, answer)
            except Exception:
                app.logger.exception("Voice reply failed; sending the text reply instead")
                send_text(user_id, answer)
        else:
            send_text(user_id, answer)
        app.logger.info("LINE final reply sent successfully")
    except Exception as error:
        app.logger.exception("LINE/Gemini message processing failed")
        try:
            send_text(user_id, "我目前暫時無法完成這則訊息，請稍後再試。系統已記錄錯誤供檢查。")
        except Exception:
            app.logger.exception("Could not send LINE error reply")


@handler.add(MessageEvent, message=TextMessageContent)
def handle_text(event):
    app.logger.info("Received LINE text event")
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text="🤖 收到，我正在整理回覆。")],
            )
        )
    threading.Thread(
        target=process_message,
        args=(event.source.user_id, event.message.text, None),
        daemon=True,
    ).start()


@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image(event):
    app.logger.info("Received LINE image event")
    with ApiClient(configuration) as api_client:
        line_api = MessagingApi(api_client)
        line_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text="🤖 收到圖片，我正在查看。")],
            )
        )
        content = MessagingApiBlob(api_client).get_message_content(event.message.id)
    image_b64 = base64.b64encode(content).decode("ascii")
    threading.Thread(
        target=process_message,
        args=(event.source.user_id, None, image_b64),
        daemon=True,
    ).start()


def process_audio_message(user_id, message_id):
    try:
        with ApiClient(configuration) as api_client:
            audio_content = MessagingApiBlob(api_client).get_message_content(message_id)
        audio_b64 = base64.b64encode(audio_content).decode("ascii")
        process_message(user_id, audio_b64=audio_b64, audio_mime_type="audio/m4a")
    except Exception:
        app.logger.exception("Could not retrieve or process LINE audio message")
        try:
            send_text(user_id, "我目前無法讀取這段語音，請稍後再傳一次，或改用文字訊息。")
        except Exception:
            app.logger.exception("Could not send LINE audio error reply")


@handler.add(MessageEvent, message=AudioMessageContent)
def handle_audio(event):
    app.logger.info("Received LINE audio event")
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text="🎧 收到語音，我正在聆聽並整理回覆。")],
            )
        )
    threading.Thread(
        target=process_audio_message,
        args=(event.source.user_id, event.message.id),
        daemon=True,
    ).start()


if __name__ == "__main__":
    if not os.getenv("LINE_CHANNEL_ACCESS_TOKEN") or not os.getenv("LINE_CHANNEL_SECRET"):
        raise RuntimeError("LINE credentials are not configured")
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not configured")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 1000)))

