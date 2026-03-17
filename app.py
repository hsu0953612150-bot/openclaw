import os
import threading
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

app = Flask(__name__)

# 讀取 Render 設定的環境變數
LINE_ACCESS_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
LINE_SECRET = os.getenv('LINE_CHANNEL_SECRET')

line_bot_api = LineBotApi(LINE_ACCESS_TOKEN)
handler = WebhookHandler(LINE_SECRET)

def openclaw_task(user_id, user_msg):
    """背景執行任務，避免 LINE 逾時"""
    try:
        # 在這裡呼叫 OpenClaw 邏輯
        # 範例：result = f"OpenClaw 成功處理：{user_msg}"
        response_text = f"✅ OpenClaw 任務完成！\n針對指令「{user_msg}」已處理完畢。"
        line_bot_api.push_message(user_id, TextSendMessage(text=response_text))
    except Exception as e:
        line_bot_api.push_message(user_id, TextSendMessage(text=f"❌ 錯誤: {str(e)}"))

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
    
    # 立即回覆，防止 LINE 報錯
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text="🤖 OpenClaw 已在雲端啟動，請稍候...")
    )
    
    # 開啟背景執行緒
    threading.Thread(target=openclaw_task, args=(user_id, user_msg)).start()

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
