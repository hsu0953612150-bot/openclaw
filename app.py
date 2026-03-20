import os
import threading
import json
import base64
import gspread
import datetime
import time
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

# --- 1. 初始化與變數清理 ---
configuration = Configuration(access_token=os.getenv('LINE_CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.getenv('LINE_CHANNEL_SECRET'))
ai_client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))
tavily = TavilyClient(api_key=os.getenv('TAVILY_API_KEY'))

# 自動過濾 SPREADSHEET_ID，只保留 ID 字串
RAW_ID = os.getenv('SPREADSHEET_ID', '')
CLEAN_ID = RAW_ID.split('/d/')[1].split('/')[0] if '/d/' in RAW_ID else RAW_ID.split('/')[0]

# --- 2. Google Sheets 穩定連線模組 ---
def get_gspread_client():
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    # 修正 Render JSON 換行符號問題
    gcp_json = os.getenv('GCP_SERVICE_ACCOUNT_JSON', '{}').replace('\\\\n', '\n').replace('\\n', '\n').strip()
    gcp_info = json.loads(gcp_json)
    creds = ServiceAccountCredentials.from_json_keyfile_dict(gcp_info, scope)
    return gspread.authorize(creds)

def get_user_memory(user_id):
    try:
        client = get_gspread_client()
        sheet = client.open_by_key(CLEAN_ID).worksheet("UserMemory")
        cell = sheet.find(user_id)
        if cell:
            # 取得 C 欄 (Index 3) 的 JSON 歷史
            val = sheet.cell(cell.row, 3).value
            return json.loads(val) if val else []
        return []
    except Exception as e:
        print(f"❌ 讀取記憶失敗: {e}")
        return []

def save_user_memory(user_id, role, content):
    try:
        client = get_gspread_client()
        sheet = client.open_by_key(CLEAN_ID).worksheet("UserMemory")
        
        history = get_user_memory(user_id)
        history.append({"role": role, "content": content})
        if len(history) > 8: history = history[-8:] # 限制 4 輪對話
        
        json_data = json.dumps(history, ensure_ascii=False)
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        cell = sheet.find(user_id)
        if cell:
            sheet.update_cell(cell.row, 3, json_data)
            sheet.update_cell(cell.row, 4, now)
        else:
            # 寫入新資料: A=UserID, B=Key, C=Value, D=Timestamp
            sheet.append_row([user_id, "chat_history", json_data, now])
        print(f"✅ 成功更新 {user_id} 的雲端記憶")
    except Exception as e:
        print(f"❌ 寫入記憶失敗: {e}")

# --- 3. 大G 任務邏輯 (聯網 + 視覺 + 記憶) ---
def gino_agent_task(user_id, user_msg=None, image_b64=None):
    with ApiClient(configuration) as api_client_line:
        line_bot_api = MessagingApi(api_client_line)
        try:
            # 讀取記憶
            history = get_user_memory(user_id)
            
            # 準備輸入
            if image_b64:
                line_bot_api.push_message(PushMessageRequest(
                    to=user_id, messages=[TextMessage(text="📸 [大G視覺模組]\n正在辨識圖片並分析資訊...")]
                ))
                search_query = "圖片物件相關技術資訊"
                user_input = [
                    {"type": "text", "text": "請根據圖片與聯網資訊回答問題。"},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}}
                ]
            else:
                line_bot_api.push_message(PushMessageRequest(
                    to=user_id, messages=[TextMessage(text="🌐 [大G聯網中]\n搜尋最新內容中...")]
                ))
                search_query = user_msg[:100]
                user_input = [{"type": "text", "text": user_msg}]

            # Tavily 搜尋
            search_res = tavily.search(query=search_query, search_depth="basic")
            context = "\n".join([f"- {r['content']}" for r in search_res['results'][:2]])

            # AI 組合回答
            sys_msg = {"role": "system", "content": f"你是「大G」，請結合搜尋結果：\n{context}\n以及雲端記憶來協助使用者。"}
            messages = [sys_msg] + history + [{"role": "user", "content": user_input}]

            response = ai_client.chat.completions.create(model="gpt-4o", messages=messages)
            answer = response.choices[0].message.content
            
            # 儲存對話至記憶
            save_user_memory(user_id, "user", user_msg if user_msg else "[傳送圖片]")
            save_user_memory(user_id, "assistant", answer)
            
            line_bot_api.push_message(PushMessageRequest(
                to=user_id, messages=[TextMessage(text=f"✅ 大G回報：\n{answer}")]
            ))
        except Exception as e:
            line_bot_api.push_message(PushMessageRequest(
                to=user_id, messages=[TextMessage(text=f"❌ 處理異常: {str(e)[:50]}")]
            ))

# --- 4. Webhook 接口 ---
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
            messages=[TextMessage(text="🤖 已接收指令，讀取雲端記憶中...")]
        ))
    threading.Thread(target=gino_agent_task, args=(event.source.user_id, event.message.text)).start()

@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image(event):
    with ApiClient(configuration) as api_client_line:
        line_bot_api = MessagingApi(api_client_line)
        line_bot_api_blob = MessagingApiBlob(api_client_line)
        line_bot_api.reply_message(ReplyMessageRequest(
            reply_token=event.reply_token,
            messages=[TextMessage(text="🤖 收到圖片，啟動視覺辨識...")]
        ))
        msg_content = line_bot_api_blob.get_message_content(event.message.id)
        img_b64 = base64.b64encode(msg_content).decode('utf-8')
    threading.Thread(target=gino_agent_task, args=(event.source.user_id, None, img_b64)).start()

if __name__ == "__main__":
    # 配合你的 Render 設定 PORT=1000
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 1000)))
