import os
import threading
import time
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from openai import OpenAI
from tavily import TavilyClient

app = Flask(__name__)

# --- 初始化 API 金鑰 (請確保 Render 的 Environment 已設定) ---
line_bot_api = LineBotApi(os.getenv('LINE_CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.getenv('LINE_CHANNEL_SECRET'))
ai_client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))
tavily = TavilyClient(api_key=os.getenv('TAVILY_API_KEY'))

# --- 簡易對話記憶體 (儲存於記憶體中) ---
user_memories = {}

def update_memory(user_id, role, content):
    if user_id not in user_memories:
        user_memories[user_id] = []
    user_memories[user_id].append({"role": role, "content": content})
    # 限制記憶長度，避免 Token 爆炸 (保留最近 6 則訊息)
    if len(user_memories[user_id]) > 12:
        user_memories[user_id] = user_memories[user_id][-12:]

def gino_agent_executor(user_id, user_msg):
    try:
        # 取得該用戶的歷史紀錄
        history = user_memories.get(user_id, [])

        # 1. 聯網搜尋：結合上下文與當前問題
        line_bot_api.push_message(user_id, TextSendMessage(text="🌐 [大G聯網中]\n正在檢索相關技術文件與背景資訊..."))
        search_query = f"User asked: {user_msg}. Context: {[m['content'] for m in history[-2:]]}"
        search_result = tavily.search(query=search_query, search_depth="advanced")
        context_data = "\n".join([f"- {r['content']}" for r in search_result['results'][:3]])

        # 2. 思考歷程：讓 AI 根據搜尋結果規劃步驟
        messages = [
            {"role": "system", "content": "你叫做「大G」，是一位精通 OpenClaw 與 Python 的技術專家。請參考搜尋結果，列出處理此問題的 3 個邏輯步驟。"}
        ] + history + [{"role": "user", "content": f"搜尋參考:\n{context_data}\n\n當前問題: {user_msg}"}]

        thought_comp = ai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages
        )
        thoughts = thought_comp.choices[0].message.content
        line_bot_api.push_message(user_id, TextSendMessage(text=f"🧠 [大G思考歷程]\n{thoughts}"))

        # 3. 最終回答：正式回覆用戶
        final_comp = ai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages + [{"role": "assistant", "content": thoughts}]
        )
        answer = final_comp.choices[0].message.content
        
        # 更新記憶
        update_memory(user_id, "user", user_msg)
        update_memory(user_id, "assistant", answer)

        line_bot_api.push_message(user_id, TextSendMessage(text=f"✅ 大G回報：\n{answer}"))

    except Exception as e:
        line_bot_api.push_message(user_id, TextSendMessage(text=f"❌ 大G模組異常: {str(e)}"))

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
    
    # 立即回覆回條
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text="🤖 大G已接收指令，正在啟動聯網思考模組...")
    )
    
    # 啟動非同步執行緒
    threading.Thread(target=gino_agent_executor, args=(user_id, user_msg)).start()

if __name__ == "__main__":
    # Render 會自動偵測 PORT 環境變數
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
