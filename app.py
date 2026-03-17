import os
import threading
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

# 假設使用 OpenClaw 的基礎爬蟲功能
try:
    from openclaw.client import OpenClawClient
except ImportError:
    # 如果是精簡版，可根據實際 OpenClaw 結構調整引用路徑
    OpenClawClient = None

app = Flask(__name__)

# 從 Render 環境變數讀取金鑰
LINE_ACCESS_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
LINE_SECRET = os.getenv('LINE_CHANNEL_SECRET')

line_bot_api = LineBotApi(LINE_ACCESS_TOKEN)
handler = WebhookHandler(LINE_SECRET)

def openclaw_worker(user_id, command):
    """在背景執行的 OpenClaw 任務"""
    try:
        # 這裡模擬 OpenClaw 的執行邏輯
        # 實際使用時請參考 OpenClaw 文件呼叫其 API
        # 例如: client = OpenClawClient(api_key=...)
        # result = client.run_task(command)
        
        # 範例回傳內容
        status_msg = f"✅ OpenClaw 任務完成！\n指令：{command}\n結果：已成功抓取數據。"
        
        # 使用 Push Message 主動傳回給使用者
        line_bot_api.push_message(user_id, TextSendMessage(text=status_msg))
    except Exception as e:
        error_msg = f"❌ 執行出錯：{str(e)}"
        line_bot_api.push_message(user_id, TextSendMessage(text=error_msg))

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
    
    # 1. 立即回覆，避免 LINE Webhook 逾時
    line_bot_api.reply_message(
        event.reply_token, 
        TextSendMessage(text="🤖 OpenClaw 已啟動，請稍候...")
    )
    
    # 2. 開啟新執行緒處理耗時的爬蟲任務
    task_thread = threading.Thread(target=openclaw_worker, args=(user_id, user_msg))
    task_thread.start()

if __name__ == "__main__":
    # Render 預設使用 PORT 10000
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
