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

# --- 1. 服務狀態檢查路徑 (部署後請瀏覽此網址測試) ---
@app.route("/", methods=['GET'])
def health_check():
    return "✅ 大G 服務目前在線！請確保 LINE Webhook URL 設定正確。", 200

# --- 2. 初始化與環境變數清理 ---
configuration = Configuration(access_token=os.getenv('LINE_CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.getenv('LINE_CHANNEL_SECRET'))
ai_client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))
tavily = TavilyClient(api_key=os.getenv('TAVILY_API_KEY'))

# 試算表 ID 處理：自動從網址或純 ID 中提取
RAW_ID = os.getenv('SPREADSHEET_ID', '')
CLEAN_ID = RAW_ID.split('/d/')[1].split('/')[0] if '/d/' in RAW_ID else RAW_ID

# --- 3. Google Sheets 記憶模組 (專治私鑰轉義錯誤) ---
def get_gspread_client():
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    gcp_json_raw = os.getenv('GCP_SERVICE_ACCOUNT_JSON', '{}')
    
    # 【關鍵】強制將 Render 環境變數中可能被誤轉義的 \\n 變回真正的換行符
    gcp_json = gcp_json_raw.replace('\\\\n', '\n').replace('\\n', '\n').strip()
    
    try:
        gcp_info = json.loads(gcp_json)
        creds = ServiceAccountCredentials.from_json_keyfile_dict(gcp_info, scope)
        return gspread.authorize(creds)
    except Exception as e:
        print(f"🔥 GCP JSON 解析失敗: {e}")
        raise

def get_user_memory(user_id):
    try:
        client = get_gspread_client()
        sheet = client.open_by_key(CLEAN_ID).worksheet("UserMemory")
        cell = sheet.find(user_id)
        if cell:
            # 讀取第 3 欄 (C) 的 JSON 歷史
            val = sheet.cell(cell.row, 3).value
            return json.loads(val) if val else []
    except Exception as e:
        print(f"⚠️ 記憶讀取失敗 (新用戶或表單未就緒): {e}")
    return []

def save_user_memory(user_id, role, content):
    try:
        client = get_gspread_client()
        sheet = client.open_by_key(CLEAN_ID).worksheet("UserMemory")
        
        history = get_user_memory(user_id)
        history.append({"role": role, "content": content})
        history = history[-10:] # 僅保留最近 5 輪對話
        
        json_data = json.dumps(history, ensure_ascii=False)
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        cell = sheet.find(user_id)
        if cell:
            sheet.update_cell(cell.row, 3, json_data)
            sheet.update_cell(cell.row, 4, now)
        else:
            # A:UserID, B:Key, C:Value, D:Timestamp
            sheet.append_row([user_id, "chat_history", json_data, now])
        print(f"✅ 成功同步記憶至 Google Sheets: {user_id}")
    except Exception as e:
        print(f"❌ 試算表寫入失敗: {e}")

# --- 4. 大G AI 思考任務 ---
def gino_agent_task(user_id, user_msg=None, image_b64=None):
    with ApiClient(configuration) as api_client_line:
        line_bot_api = MessagingApi(api_client_line)
        try:
            # 讀取雲端記憶
            history = get_user_memory(user_id)
            
            # 處理輸入類型
            if image_b64:
                line_bot_api.push_message(PushMessageRequest(
                    to=user_id, messages=[TextMessage(text="📸 [大G視覺] 正在看圖思考...")]
                ))
                search_query = "辨識此圖中物體"
                user_content = [
                    {"type": "text", "text": "請結合圖片與搜尋結果回答。"},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}}
                ]
            else:
                line_bot_api.push_message(PushMessageRequest(
                    to=user_id, messages=[TextMessage(text="🌐 [大G聯網] 搜集中...")]
                ))
                search_query = user_msg[:50]
                user_content = [{"type": "text", "text": user_msg}]

            # Tavily 搜尋
            search_res = tavily.search(query=search_query, search_depth="basic")
            context = "\n".join([r['content'] for r in search_res['results'][:2]])

            # GPT-4o 生成
            sys_prompt = {
                "role": "system", 
                "content": f"你叫「大G」。現在時間：{datetime.datetime.now()}。搜尋參考資料：\n{context}\n⚠️警告：請嚴格遵守『雲端記憶』中的使用者資訊（如位置、偏好），不要被搜尋結果誤導。"
            }
            
            messages = [sys_prompt] + history + [{"role": "user", "content": user_content}]
            response = ai_client.chat.completions.create(model="gpt-4o", messages=messages)
            answer = response.choices[0].message.content
            
            # 存入記憶
            save_user_memory(user_id, "user", user_msg if user_msg else "[傳送圖片]")
            save_user_memory(user_id, "assistant", answer)
            
            line_bot_api.push_message(PushMessageRequest(
                to=user_id, messages=[TextMessage(text=f"✅ 大G回報：\n{answer}")]
            ))
        except Exception as e:
            line_bot_api.push_message(PushMessageRequest(
                to=user_id, messages=[TextMessage(text=f"❌ 錯誤: {str(e)[:100]}")]
            ))

# --- 5. LINE Webhook 端點 ---
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
            messages=[TextMessage(text="🤖 已收到指令，正在連線雲端記憶庫...")]
        ))
    threading.Thread(target=gino_agent_task, args=(event.source.user_id, event.message.text)).start()

@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image(event):
    with ApiClient(configuration) as api_client_line:
        line_bot_api = MessagingApi(api_client_line)
        line_bot_api_blob = MessagingApiBlob(api_client_line)
        line_bot_api.reply_message(ReplyMessageRequest(
            reply_token=event.reply_token,
            messages=[TextMessage(text="🤖 收到圖片，啟動大G視覺分析...")]
        ))
        msg_content = line_bot_api_blob.get_message_content(event.message.id)
        img_b64 = base64.b64encode(msg_content).decode('utf-8')
    threading.Thread(target=gino_agent_task, args=(event.source.user_id, None, img_b64)).start()

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 1000)))
