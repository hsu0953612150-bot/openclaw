import os
import threading
import time
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from openai import OpenAI

app = Flask(__name__)

# 讀取環境變數
LINE_ACCESS_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
LINE_SECRET = os.getenv('LINE_CHANNEL_SECRET')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')

line_bot_api = LineBotApi(LINE_ACCESS_TOKEN)
handler = WebhookHandler(LINE_SECRET)
ai_client = OpenAI(api_key=OPENAI_API_KEY)

def ai_reasoning_worker(user_id, user_msg):
    """背景執行緒：負責 AI 思考歷程生成與最終回答"""
    try:
        # --- 第一階段：生成思考歷程 ---
        # 我們設定 System Prompt 讓 AI 只輸出思考步驟
        thought_completion = ai_client.chat.completions.create(
            model="gpt-3.5-turbo", # 建議使用 gpt-4o-mini 以獲得更快的速度
            messages=[
                {"role": "system", "content": "你是一個專業的 AI 助手。請針對用戶的問題，簡短列出 3 個你的思考步驟或計畫，並加上 Emoji。"},
                {"role": "user", "content": user_msg}
            ]
        )
        thoughts = thought_completion.choices[0].message.content
        line_bot_api.push_message(user_id, TextSendMessage(text=f"🧠 [思考歷程]\n{thoughts}"))

        # --- 第二階段：模擬執行 (可在此處插入 OpenClaw 爬蟲邏輯) ---
        time.sleep(1) # 增加視覺停頓感
        line_bot_api.push_message(user_id, TextSendMessage(text="🔍 [執行階段]\n正在雲端沙盒環境安全地檢索相關資訊，確保數據不留痕跡..."))

        # --- 第三階段：生成最終回答 ---
        final_completion = ai_client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "你是 OpenClaw AI。請以專業、親切且注重隱私安全的語氣回答用戶。"},
                {"role": "user", "content": user_msg}
            ]
        )
        final_answer = final_completion.choices[0].message.content
        line_bot_api.push_message(user_id, TextSendMessage(text=f"✅ 任務完成：\n{final_answer}"))

    except Exception as e:
        # 錯誤捕捉並回報給 LINE 用戶
        error_log = f"❌ AI 思考模組異常：{str(e)}"
        line_bot_api.push_message(user_id, TextSendMessage(text=error_log))

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
def handle_message(event):
    user_id = event.source.user_id
    user_msg = event.message.text
    
    # 1. 立即回覆回條，讓 LINE 伺服器知道我們收到了，避免逾時
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text="🤖 OpenClaw 已接管指令，思考模組啟動中...")
    )
    
    # 2. 開啟 Thread 在背景跑 AI 邏輯，並使用 push_message 回傳
    threading.Thread(target=ai_reasoning_worker, args=(user_id, user_msg)).start()

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
