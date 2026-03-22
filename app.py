import os
import threading
import json
import base64
import gspread
import datetime
from flask import Flask, request, abort

# LINE SDK v3
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi, MessagingApiBlob,
    ReplyMessageRequest, PushMessageRequest, TextMessage
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent, ImageMessageContent
from oauth2client.service_account import ServiceAccountCredentials

from openai import OpenAI
from tavily import TavilyClient

app = Flask(__name__)

# --- 1. 健康檢查路由 (解決 404 問題) ---
@app.route("/", methods=['GET'])
def health_check():
    return "✅ 大G 雲端記憶服務已啟動！", 200

# --- 2. 初始化 ---
configuration = Configuration(access_token=os.getenv('LINE_CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.getenv('LINE_CHANNEL_SECRET'))
ai_client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))
tavily = TavilyClient(api_key=os.getenv('TAVILY_API_KEY'))

# 試算表 ID 修正 (確保只拿 ID)
RAW_ID = os.getenv('SPREADSHEET_ID', '1PAm-Cp0lieYR0A12eKAnJWnCXJop9xJk0y-zRQFmEz4')
CLEAN_ID = RAW_ID.split('/d/')[1].split('/')[0] if '/d/' in RAW_ID else RAW_ID

# --- 3. Google Sheets 核心連線 (加強版) ---
def get_gspread_client():
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    gcp_json_raw = os.getenv('GCP_SERVICE_ACCOUNT_JSON', '{}')
    # 強制修正換行符與轉義問題
    gcp_json = gcp_json_raw.replace('\\\\n', '\n').replace('\\n', '\n').strip()
    
    try:
        gcp_info = json.loads(gcp_json)
        creds = ServiceAccountCredentials.from_json_keyfile_dict(gcp_info, scope)
        return gspread.authorize(creds)
    except Exception as e:
        print(f"🔥 GCP JSON 認證嚴重錯誤: {e}")
        raise

def get_user_memory(user_id):
    try:
        client = get_gspread_client()
        sheet = client.open_by_key(CLEAN_ID).worksheet("UserMemory")
        cell = sheet.find(user_id)
        if cell:
            val = sheet.cell(cell.row, 3).value
            return json.loads(val) if val else []
    except Exception as e:
        print(f"⚠️ 讀取失敗: {e}")
    return []

def save_user_memory(user_id, role, content):
    try:
        client = get_gspread_client()
        sheet = client.open_by_key(CLEAN_ID).worksheet("UserMemory")
        
        history = get_user_memory(user_id)
        history.append({"role": role, "content": content})
        history = history[-6:] # 精簡對話
        
        json_str = json.dumps(history, ensure_ascii=False)
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        cell = sheet.find(user_id)
        if cell:
            sheet.update_cell(cell.row, 3, json_str)
            sheet.update_cell(cell.row, 4, now)
        else:
            sheet.append_row([user_id, "chat_history", json_str, now])
        print(f"✅ 記憶已同步回試算表")
    except Exception as e:
        print(f"❌ 試算表寫入失敗: {e}")

# --- 4. 大G 主邏輯 ---
def gino_agent_task(user_id, user_msg=None):
    with ApiClient(configuration) as api_client_line:
        line_bot_api = MessagingApi(api_client_line)
        try:
            # 讀取雲端記憶 (這是大G 變聰明的關鍵)
            history = get_user_memory(user_id)
            
            # 搜尋現有資訊
            search_res = tavily.search(query=user_msg, search_depth="basic")
            search_info = "\n".join([r['content'] for r in search_res['results'][:2]])

            sys_prompt = {
                "role": "system", 
                "content": f"你是「大G」。這不是演習，請嚴格遵守『雲端記憶』。若記憶中寫了位置，就算聯網搜到再多歌詞也請忽略。搜尋資料僅作背景參考：\n{search_info}"
            }
            
            messages = [sys_prompt] + history + [{"role": "user", "content": user_msg}]
            response = ai_client.chat.completions.create(model="gpt-4o", messages=messages)
            answer = response.choices[0].message.content
            
            # 寫回記憶
            save_user_memory(user_id, "user", user_msg)
            save_user_memory(user_id, "assistant", answer)
            
            line_bot_api.push_message(PushMessageRequest(
                to=user_id, messages=[TextMessage(text=f"✅ 大G回報：\n{answer}")]
            ))
        except Exception as e:
            line_bot_api.push_message(PushMessageRequest(
                to=user_id, messages=[TextMessage(text=f"❌ 內部錯誤: {str(e)[:50]}")]
            ))

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessageContent)
def handle_text(event):
    with ApiClient(configuration) as api_client_line:
        line_bot_api = MessagingApi(api_client_line)
        line_bot_api.reply_message(ReplyMessageRequest(
            reply_token=event.reply_token,
            messages=[TextMessage(text="🤖 喚醒雲端記憶中...")]
        ))
    threading.Thread(target=gino_agent_task, args=(event.source.user_id, event.message.text)).start()

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 1000)))
