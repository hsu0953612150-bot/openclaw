import os
import threading
import json
import base64
import gspread
import datetime
from flask import Flask, request, abort
from oauth2client.service_account import ServiceAccountCredentials
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, ImageMessage
from openai import OpenAI
from tavily import TavilyClient

app = Flask(__name__)

# --- 1. 服務初始化 ---
line_bot_api = LineBotApi(os.getenv('LINE_CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.getenv('LINE_CHANNEL_SECRET'))
ai_client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))
tavily = TavilyClient(api_key=os.getenv('TAVILY_API_KEY'))

# --- 2. Google Sheets 雲端記憶模組 ---
def get_user_memory(user_id):
    try:
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        # 自動處理 JSON 字串中的換行，增加穩定性
        gcp_json = os.getenv('GCP_SERVICE_ACCOUNT_JSON').replace('\n', '').strip()
        gcp_info = json.loads(gcp_json)
        creds = ServiceAccountCredentials.from_json_keyfile_dict(gcp_info, scope)
        client = gspread.authorize(creds)
        
        # 開啟試算表並指定 UserMemory 分頁
        sheet = client.open_by_key(os.getenv('SPREADSHEET_ID')).worksheet("UserMemory")
        cell = sheet.find(user_id)
        
        if cell:
            val = sheet.cell(cell.row, 3).value # 取得 Value 欄位的對話紀錄
            return json.loads(val) if val else []
        return []
    except Exception as e:
        print(f"讀取記憶失敗: {e}")
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
        if len(history) > 10: history = history[-10:] # 僅保留最近 5 輪對話，節省 Token
        
        json_history = json.dumps(history, ensure_ascii=False)
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        cell = sheet.find(user_id)
        if cell:
            sheet.update_cell(cell.row, 3, json_history)
            sheet.update_cell(cell.row, 4, now)
        else:
            # 對應您的 CSV 架構：UserID, Key, Value, Timestamp
            sheet.append_row([user_id, "conversation_history", json_history, now])
    except Exception as e:
        print(f"儲存記憶失敗: {e}")

# --- 3. 大G 核心任務 (聯網 + 視覺 + 記憶) ---
def gino_agent_task(user_id, user_msg=None, image_b64=None):
    try:
        history = get_user_memory(user_id)
        
        # 決定輸入與搜尋關鍵字 (限制字數防錯)
        if image_b64:
            line_bot_api.push_message(user_id, TextSendMessage(text="📸 [大G視覺辨識]\n正在分析圖片並檢索技術背景..."))
            search_query = "技術內容深度分析"
            user_input_content = [
                {"type": "text", "text": "請詳細分析這張圖片，並結合聯網資訊回答。"},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}}
            ]
        else:
            line_bot_api.push_message(user_id, TextSendMessage(text="🌐 [大G聯網中]\n搜尋最新精準資訊..."))
            search_query = user_msg[:120] if user_msg else "技術諮詢"
            user_input_content = [{"type": "text", "text": user_msg}]

        # Tavily 搜尋
        search_res = tavily.search(query=search_query, search_depth="basic")
        context = "\n".join([f"- {r['content']}" for r in search_res['results'][:2]])

        # 組合 OpenAI Messages
        system_msg = {"role": "system", "content": "你叫「大G」，是一位精通 Python 的技術專家。請參考搜尋結果與雲端記憶回答。"}
        
        if image_b64:
            # 視覺模式：將搜尋背景塞入 text 欄位
            user_input_content[0]["text"] = f"搜尋參考:\n{context}\n\n{user_input_content[0]['text']}"
            messages = [system_msg] + history + [{"role": "user", "content": user_input_content}]
        else:
            messages = [system_msg] + history + [{"role": "user", "content": f"搜尋參考:\n{context}\n\n指令: {user_msg}"}]

        # 生成回覆
        response = ai_client.chat.completions.create(model="gpt-4o", messages=messages)
        answer = response.choices[0].message.content
        
        # 存回記憶
        save_user_memory(user_id, "user", user_msg if user_msg else "[發送圖片]")
        save_user_memory(user_id, "assistant", answer)
        
        line_bot_api.push_message(user_id, TextSendMessage(text=f"✅ 大G回報：\n{answer}"))
        
    except Exception as e:
        print(f"Task Error: {e}")
        line_bot_api.push_message(user_id, TextSendMessage(text=f"❌ 系統異常: {str(e)[:100]}"))

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

@handler.add(MessageEvent, message=TextMessage)
def handle_text(event):
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text="🤖 指令已接收，啟動雲端記憶中..."))
    threading.Thread(target=gino_agent_task, args=(event.source.user_id, event.message.text)).start()

@handler.add(MessageEvent, message=ImageMessage)
def handle_image(event):
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text="🤖 收到圖片，啟動視覺辨識模組..."))
    message_content = line_bot_api.get_message_content(event.message.id)
    img_b64 = base64.b64encode(message_content.content).decode('utf-8')
    threading.Thread(target=gino_agent_task, args=(event.source.user_id, None, img_b64)).start()

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 10000)))
