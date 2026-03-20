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

# --- 1. 服務與 API 初始化 ---
configuration = Configuration(access_token=os.getenv('LINE_CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.getenv('LINE_CHANNEL_SECRET'))
ai_client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))
tavily = TavilyClient(api_key=os.getenv('TAVILY_API_KEY'))

# 處理 SPREADSHEET_ID，確保只保留 ID 字串部分
RAW_ID = os.getenv('SPREADSHEET_ID', '')
CLEAN_ID = RAW_ID.split('/d/')[1].split('/')[0] if '/d/' in RAW_ID else RAW_ID.split('/')[0]

# --- 2. Google Sheets 記憶存取模組 ---
def get_gspread_client():
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    # 【關鍵修正】解決 Render 環境變數轉義導致的私鑰解析錯誤
    gcp_json_raw = os.getenv('GCP_SERVICE_ACCOUNT_JSON', '{}')
    gcp_json = gcp_json_raw.replace('\\\\n', '\n').replace('\\n', '\n').strip()
    
    try:
        gcp_info = json.loads(gcp_json)
        creds = ServiceAccountCredentials.from_json_keyfile_dict(gcp_info, scope)
        return gspread.authorize(creds)
    except Exception as e:
        print(f"🔥 GCP JSON 解析出錯: {e}")
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
        print(f"⚠️ 讀取記憶失敗: {e}")
    return []

def save_user_memory(user_id, role, content):
    try:
        client = get_gspread_client()
        sheet = client.open_by_key(CLEAN_ID).worksheet("UserMemory")
        
        # 獲取並更新歷史 (保留最近 8 則訊息)
        history = get_user_memory(user_id)
        history.append({"role": role, "content": content})
        history = history[-8:] 
        
        json_data = json.dumps(history, ensure_ascii=False)
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        cell = sheet.find(user_id)
        if cell:
            sheet.update_cell(cell.row, 3, json_data)
            sheet.update_cell(cell.row, 4, now)
        else:
            # A:UserID, B:Key, C:Value, D:Timestamp
            sheet.append_row([user_id, "chat_history", json_data, now])
        print(f"✅ 成功將對話同步至試算表: {user_id}")
    except Exception as e:
        print(f"❌ 試算表寫入失敗: {e}")

# --- 3. 大G 核心任務邏輯 (聯網+視覺+記憶) ---
def gino_agent_task(user_id, user_msg=None, image_b64=None):
    with ApiClient(configuration) as api_client_line:
        line_bot_api = MessagingApi(api_client_line)
        try:
            # 1. 讀取長期記憶
            history = get_user_memory(user_id)
            
            # 2. 判斷輸入類型與搜尋預算
            if image_b64:
                line_bot_api.push_message(PushMessageRequest(
                    to=user_id, messages=[TextMessage(text="📸 [大G視覺辨識]\n正在分析圖片並檢索技術資料...")]
                ))
                search_query = "圖片中的物體或場景技術細節分析"
                user_content = [
                    {"type": "text", "text": "請根據圖片內容與聯網搜尋結果回答問題。"},
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

            # 4. GPT-4o 決策生成 (強化記憶優先級)
            sys_prompt = {
                "role": "system", 
                "content": f"你是「大G」，目前的狀態請優先參考『雲端記憶』。現在時間：{datetime.datetime.now()}。搜尋參考資料：\n{search_context}\n⚠️ 嚴禁：若記憶中有提到位置，以記憶為準，不要被搜尋到的歌詞、廣告或無關新聞誤導。"
            }
            
            messages = [sys_prompt] + history + [{"role": "user", "content": user_content}]
            response = ai_client.chat.completions.create(model="gpt-4o", messages=messages)
            answer = response.choices[0].message.content
            
            # 5. 回傳結果並持久化存儲
            save_user_memory(user_id, "user", user_msg if user_msg else "[圖片訊息]")
            save_user_memory(user_id, "assistant", answer)
            
            line_bot_api.push_message(PushMessageRequest(
                to=user_id, messages=[TextMessage(text=f"✅ 大G回報：\n{answer}")]
            ))
        except Exception as e:
            line_bot_api.push_message(PushMessageRequest(
                to=user_id, messages=[TextMessage(text=f"❌ 系統異常: {str(e)[:100]}")]
            ))

# --- 4. Webhook 端點 ---
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
            messages=[TextMessage(text="🤖 已收到指令，正在喚醒雲端記憶...")]
        ))
    threading.Thread(target=gino_agent_task, args=(event.source.user_id, event.message.text)).start()

@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image(event):
    with ApiClient(configuration) as api_client_line:
        line_bot_api = MessagingApi(api_client_line)
        line_bot_api_blob = MessagingApiBlob(api_client_line)
        line_bot_api.reply_message(ReplyMessageRequest(
            reply_token=event.reply_token,
            messages=[TextMessage(text="🤖 收到圖片，啟動大G視覺模組...")]
        ))
        msg_content = line_bot_api_blob.get_message_content(event.message.id)
        img_b64 = base64.b64encode(msg_content).decode('utf-8')
    threading.Thread(target=gino_agent_task, args=(event.source.user_id, None, img_b64)).start()

if __name__ == "__main__":
    # 確保 Port 設定為 1000 以匹配你的 Render 設定
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 1000)))
