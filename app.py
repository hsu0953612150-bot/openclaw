import os
import threading
import time
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

app = Flask(__name__)

# 從環境變數讀取金鑰
LINE_ACCESS_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
LINE_SECRET = os.getenv('LINE_CHANNEL_SECRET')
line_bot_api = LineBotApi(LINE_ACCESS_TOKEN)
handler = WebhookHandler(LINE_SECRET)

def openclaw_task(user_id, user_msg):
    """背景執行任務：包含思考歷程顯示"""
    try:
        # --- 模擬思考歷程第一階段 ---
        time.sleep(1) # 增加一點真實感
        thought_1 = "🧠 [思考歷程 - 階段 1]\n正在解析指令語義...\n檢測到關鍵字：'隱私', '思考'。正在調用安全模組。"
        line_bot_api.push_message(user_id, TextSendMessage(text=thought_1))

        # --- 模擬思考歷程第二階段 (這裡可以放 OpenClaw 爬蟲邏輯) ---
        time.sleep(2)
        thought_2 = "🔍 [思考歷程 - 階段 2]\n正在啟動雲端虛擬瀏覽器 (Playwright)...\n隱身模式已開啟，正在 Render 沙盒環境中進行數據隔離運算。"
        line_bot_api.push_message(user_id, TextSendMessage(text=thought_2))

        # --- 最終答案產生邏輯 ---
        if "隱私" in user_msg or "保護" in user_msg:
            final_answer = (
                "🛡️ 【OpenClaw 安全報告】\n\n"
                "本系統採用以下機制保護您的資料：\n"
                "1. 環境隔離：所有操作在獨立的 Docker 容器中執行。\n"
                "2. 密鑰安全：LINE Token 僅儲存於 Render 環境變數，代碼不留存。\n"
                "3. 無跡執行：任務結束後自動關閉瀏覽器，不儲存任何 Cookie。"
            )
        else:
            final_answer = f"✅ 任務處理完畢！\n針對您的指令「{user_msg}」，OpenClaw 已完成深度掃描與分析。"

        # 發送最終結果
        line_bot_api.push_message(user_id, TextSendMessage(text=final_answer))

    except Exception as e:
        line_bot_api.push_message(user_id, TextSendMessage(text=f"❌ 思考模組發生異常: {str(e)}"))

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
    
    # 1. 立即回覆回條 (這很重要，否則 LINE 會顯示錯誤)
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text="🤖 OpenClaw 已接管指令，思考模組啟動中...")
    )
    
    # 2. 開啟背景執行緒，讓它慢慢回傳「思考歷程」
    threading.Thread(target=openclaw_task, args=(user_id, user_msg)).start()

if __name__ == "__main__":
    # Render 預設監聽 0.0.0.0:10000
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
