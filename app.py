import base64
import datetime
import json
import os
import threading
import urllib.error
import urllib.request

import gspread
from flask import Flask, abort, request
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
from linebot.v3.webhooks import ImageMessageContent, MessageEvent, TextMessageContent
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
MAX_HISTORY_MESSAGES = max(2, int(os.getenv("MAX_HISTORY_MESSAGES", "8")))

SYSTEM_INSTRUCTION = """你是 LINE 官方帳號「大G」的 AI 助理，使用繁體中文回答。
只回答使用者實際提出的問題。若使用者只說「測試訊息」、問候或沒有提出具體問題，簡短確認收到即可，不要猜測主題或補充無關建議。
回答清楚、親切、精簡；不要聲稱已保存資料或完成外部操作，除非系統已明確完成。"""

_sheet_lock = threading.Lock()


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


def gemini_generate(history, image_b64=None):
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not configured on the service")
    contents = []
    for message in history[-MAX_HISTORY_MESSAGES:]:
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

    body = json.dumps({
        "systemInstruction": {"parts": [{"text": SYSTEM_INSTRUCTION}]},
        "contents": contents,
        "generationConfig": {"maxOutputTokens": 800},
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


def process_message(user_id, user_text=None, image_b64=None):
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
        answer = gemini_generate(history, image_b64=image_b64)
        history.append({"role": "assistant", "content": answer})
        try:
            save_history(user_id, history)
        except Exception:
            app.logger.exception("Google Sheets write failed; AI reply will still be sent")
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


if __name__ == "__main__":
    if not os.getenv("LINE_CHANNEL_ACCESS_TOKEN") or not os.getenv("LINE_CHANNEL_SECRET"):
        raise RuntimeError("LINE credentials are not configured")
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not configured")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 1000)))

