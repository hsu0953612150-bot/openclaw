import os
import threading
import json
import base64
import gspread
import datetime
import time
from flask import Flask, request, abort

# LINE SDK v3 最新元件
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

# --- 1. 初始化與環境變數處理 ---
configuration = Configuration(access_token=os.getenv('LINE_CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.getenv('LINE_CHANNEL_SECRET'))
ai_client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))
tavily = TavilyClient(api_key=os.getenv('TAVILY_API_KEY'))

# 清理 SPREADSHEET_ID：如果包含網址，自動切出 ID 部分
RAW_ID = os.getenv('SPREADSHEET_ID', '')
CLEAN_SHEET_ID = RAW_ID.split('/d/')[1].split('/')[0] if '/d/' in RAW_ID else RAW_ID.split('/')[0]

# --- 2. Google Sheets 記憶模組 ---
def get_gspread_client():
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    # 關鍵：處理 Render 環境變數可能產生的雙斜線換行符號
    gcp_json = os.getenv('GCP_SERVICE_ACCOUNT_JSON', '{}').replace('\\\\n', '\n').replace('\\n', '\n').strip()
    gcp_info = json.loads(gcp_json)
    creds = ServiceAccountCredentials.from_json_keyfile_dict(gcp_info, scope)
    return gspread.authorize(creds)

def get_user_memory(user_id):
    try:
        client = get_gspread_client()
        sheet = client.open_by_key(CLEAN_SHEET_ID).worksheet("UserMemory")
        cell = sheet.find(user_id)
        if cell:
            # 欄位 C (Index 3) 是 Value
            val = sheet.cell(cell.row, 3).value
            return json.loads(val) if val else []
        return []
    except Exception as e:
        print(f"⚠️ 讀取記憶失敗: {e}")
        return []

def save_user_memory(user_id, role, content):
    try:
        client = get_gspread_client()
        sheet = client.open_by_key(CLEAN_SHEET_ID).worksheet("UserMemory")
        
        # 取得現有歷史並更新
        history = get_user_memory(user_id)
        history.append({"role": role, "content": content})
        if len(history) > 10: history = history[-10:] # 限制最近 5 輪對話
        
        json_data = json.dumps(history, ensure_ascii=False)
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        cell = sheet.find(user_id)
        if cell:
            sheet.update_cell(cell.row, 3, json_data)
            sheet.update_cell(cell.row, 4, now)
        else:
            # A:UserID, B:Key, C:Value, D:Timestamp
            sheet.append_row([user_id, "chat_history", json_data, now])
        print(f"✅ 記憶已儲存: {user_id}")
    except Exception as e:
        print(f"⚠️ 儲存記憶失敗: {e}")

# --- 3. 大G 核心 AI 邏輯 ---
def gino_agent_task(user_id, user_msg=None, image_b64=None):
    with ApiClient(configuration) as api_client_line:
        line_bot_api = MessagingApi(api_client_line)
        try:
            # 1. 讀取記憶
            history = get_user_memory(user_id)
            
            # 2. 準備輸入與搜尋
            if image_b64:
                line_bot_api.push_message(PushMessageRequest(
                    to=user_id, messages=[TextMessage(text="📸 [大G視覺辨識]\n正在分析圖片內容...")]
                ))
                search_query = "圖片內容技術分析"
                user_content = [
                    {"type": "text", "text": "請詳細分析圖片並結合聯網資訊回答。"},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}}
                ]
            else:
                line_bot_api.push_message(PushMessageRequest(
                    to=user_id, messages=[TextMessage(text="🌐 [大G聯網中]\n搜尋最新精準資訊...")]
                ))
                search_query = user_msg[:100]
                user_content = [{"type": "text", "text": user_msg}]

            # 3. 聯網搜尋 (Tavily)
            search_res = tavily.search(query=search_query, search_depth="basic")
            search_context = "\n".join([f"- {r['content']}" for r in search_res['results'][:2]])

            # 4. OpenAI GPT-4o 組合回答
            system_prompt = {
                "role": "system", 
                "content": f"你叫「大G」，是個專家助手。現在時間是 {datetime.datetime.now()}。請結合『雲端記憶』與『搜尋結果』提供精確回答。"
            }
            
            # 組合上下文
            context_msg = {"role": "system", "content": f"最新搜尋資訊:\n{search_context}"}
            messages = [system_prompt] + history + [context_msg] + [{"role": "user", "content": user_content}]

            response = ai_client.chat.completions.create(model="gpt-4o", messages=messages)
            answer = response.choices[0].message.content
            
            # 5. 存入記憶並推送回覆
            save_user_memory(user_id, "user", user_msg if user_msg else "[傳送了圖片]")
            save_user_memory(user_id, "assistant", answer)
            
            line_bot_api.push_message(PushMessageRequest(
                to=user_id, messages=[TextMessage(text=f"✅ 大G回報：\n{answer}")]
            ))
        except Exception as e:
            error_msg = str(e)[:100]
            line_bot_api.push_message(PushMessageRequest(
                to=user_id, messages=[TextMessage(text=f"❌ 系統異常: {error_msg}")]
            ))

# --- 4. Webhook 路由 ---
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
            messages=[TextMessage(text="🤖 已接收指令，讀取記憶中...")]
        ))
    threading.Thread(target=gino_agent_task, args=(event.source.user_id, event.message.text)).start()

@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image(event):
    with ApiClient(configuration) as api_client_line:
        line_bot_api = MessagingApi(api_client_line)
        line_bot_api_blob = MessagingApiBlob(api_client_line)
        line_bot_api.reply_message(ReplyMessageRequest(
            reply_token=event.reply_token,
            messages=[TextMessage(text="🤖 收到圖片，啟動視覺模組...")]
        ))
        # 下載圖片內容
        msg_content = line_bot_api_blob.get_message_content(event.message.id)
        img_b64 = base64.b64encode(msg_content).decode('utf-8')
    threading.Thread(target=gino_agent_task, args=(event.source.user_id, None, img_b64)).start()

if __name__ == "__main__":
    # Render 會自動提供 PORT 環境變數
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
