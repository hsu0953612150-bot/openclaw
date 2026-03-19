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

# --- 1. 服務初始化 ---
configuration = Configuration(access_token=os.getenv('LINE_CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.getenv('LINE_CHANNEL_SECRET'))
ai_client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))
tavily = TavilyClient(api_key=os.getenv('TAVILY_API_KEY'))

# --- 2. Google Sheets 記憶模組 ---
def get_user_memory(user_id):
    try:
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        gcp_json = os.getenv('GCP_SERVICE_ACCOUNT_JSON').replace('\n', '').strip()
        gcp_info = json.loads(gcp_json)
        creds = ServiceAccountCredentials.from_json_keyfile_dict(gcp_info, scope)
        client = gspread.authorize(creds)
        
        sheet = client.open_by_key(os.getenv('SPREADSHEET_ID')).worksheet("UserMemory")
        cell = sheet.find(user_id)
        
        if cell:
            val = sheet.cell(cell.row, 3).value
            return json.loads(val) if val else []
        return []
    except Exception as e:
        print(f"Memory Read Error: {e}")
        return []

def save_user_memory(user_id, role, content):
    try:
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        gcp_json = os.getenv('GCP_SERVICE_ACCOUNT_JSON').replace('\n', '').strip()
        gcp_info = json.loads(gcp_json)
        creds = ServiceAccountCredentials.from_json_keyfile_dict(gcp_info, scope)
        client = gspread.authorize(creds)
        sheet = client.open_by_key(os.getenv('SPREADSHEET_ID')).worksheet("UserMemory")
        
        history = get_user_memory(user_id)
        history.append({"role": role, "content": content})
        if len(history) > 10: history = history[-10:]
        
        json_history = json.dumps(history, ensure_ascii=False)
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        cell = sheet.find(user_id)
        if cell:
            sheet.update_cell(cell.row, 3, json_history)
            sheet.update_cell(cell.row, 4, now)
        else:
            sheet.append_row([user_id, "conversation_history", json_history, now])
    except Exception as e:
        print(f"Memory Save Error: {e}")

# --- 3. 大G 核心任務 ---
def gino_agent_task(user_id, user_msg=None, image_b64=None):
    with ApiClient(configuration) as api_client_line:
        line_bot_api = MessagingApi(api_client_line)
        try:
            history = get_user_memory(user_id)
            
            if image_b64:
                line_bot_api.push_message(PushMessageRequest(
                    to=user_id, messages=[TextMessage(text="📸 [大G視覺辨識]\n正在分析圖片與聯網檢索...")]
                ))
                search_query = "圖片相關技術資訊分析"
                user_input = [
                    {"type": "text", "text": "分析這張圖片內容並結合搜尋結果。"},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}}
                ]
            else:
                line_bot_api.push_message(PushMessageRequest(
                    to=user_id, messages=[TextMessage(text="🌐 [大G聯網中]\n搜尋最新精準資訊...")]
                ))
                search_query = user_msg[:100] if user_msg else "技術諮詢"
                user_input = [{"type": "text", "text": user_msg}]

            # Tavily 搜尋
            search_res = tavily.search(query=search_query, search_depth="basic")
            context = "\n".join([f"- {r['content']}" for r in search_res['results'][:2]])

            # AI 組合訊息
            system_msg = {"role": "system", "content": "你叫「大G」，是一位專業開發專家。請參考搜尋結果與雲端記憶回答。"}
            
            if image_b64:
                user_input[0]["text"] = f"搜尋背景:\n{context}\n\n{user_input[0]['text']}"
                messages = [system_msg] + history + [{"role": "user", "content": user_input}]
            else:
                messages = [system_msg] + history + [{"role": "user", "content": f"搜尋背景:\n{context}\n\n指令: {user_msg}"}]

            response = ai_client.chat.completions.create(model="gpt-4o", messages=messages)
            answer = response.choices[0].message.content
            
            save_user_memory(user_id, "user", user_msg if user_msg else "[圖片分析]")
            save_user_memory(user_id, "assistant", answer)
            
            line_bot_api.push_message(PushMessageRequest(
                to=user_id, messages=[TextMessage(text=f"✅ 大G回報：\n{answer}")]
            ))
        except Exception as e:
            line_bot_api.push_message(PushMessageRequest(
                to=user_id, messages=[TextMessage(text=f"❌ 異常: {str(e)[:100]}")]
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
            messages=[TextMessage(text="🤖 已接收指令，啟動雲端記憶...")]
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
        msg_content = line_bot_api_blob.get_message_content(event.message.id)
        img_b64 = base64.b64encode(msg_content).decode('utf-8')
    threading.Thread(target=gino_agent_task, args=(event.source.user_id, None, img_b64)).start()

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 10000)))
