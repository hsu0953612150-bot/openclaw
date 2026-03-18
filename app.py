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

# --- 對話記憶體 ---
user_memories = {}

def update_memory(user_id, role, content):
    if user_id not in user_memories:
        user_memories[user_id] = []
    user_memories[user_id].append({"role": role, "content": content})
    if len(user_memories[user_id]) > 10: # 保留最近 5 輪對話
        user_memories[user_id] = user_memories[user_id][-10:]

def gino_agent_executor(user_id, user_msg):
    try:
        history = user_memories.get(user_id, [])

        # 1. 聯網搜尋 (關鍵修復：限制 query 長度防止 400 錯誤)
        line_bot_api.push_message(user_id, TextSendMessage(text="🌐 [大G聯網中]\n正在精準檢索相關技術資訊..."))
        
        # 僅取用戶當前問題的前 200 字進行搜尋，確保不超標
        safe_query = user_msg[:200] 
        search_result = tavily.search(query=safe_query, search_depth="basic")
        context_data = "\n".join([f"- {r['content']}" for r in search_result['results'][:2]])

        # 2. 思考歷程
        system_prompt = "你叫做「大G」，是一位專業的技術助手。請參考搜尋結果並結合對話紀錄，列出 3 個處理步驟。"
        messages = [{"role": "system", "content": system_prompt}] + history + [
            {"role": "user", "content": f"搜尋參考內容:\n{context_data}\n\n我的問題: {user_msg}"}
        ]

        thought_comp = ai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages
        )
        thoughts = thought_comp.choices[0].message.content
        line_bot_api.push_message(user_id, TextSendMessage(text=f"🧠 [大G思考歷程]\n{thoughts}"))

        # 3. 最終回答
        final_comp = ai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages + [{"role": "assistant", "content": thoughts}]
        )
        answer = final_comp.choices[0].message.content
        
        # 儲存對話歷史
        update_memory(user_id, "user", user_msg)
        update_memory(user_id, "assistant", answer)

        line_bot_api.push_message(user_id, TextSendMessage(text=f"✅ 大G回報：\n{answer}"))

    except Exception as e:
        # 增加詳細錯誤紀錄輸出到 Render Logs
        print(f"Error Details: {str(e)}")
        line_bot_api.push_message(user_id, TextSendMessage(text=f"❌ 大G模組異常: {str(e)[:100]}"))

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
    # 立即回覆回條
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text="🤖 大G已接收指令，正在啟動聯網思考模組...")
    )
    # 啟動非同步執行緒
    threading.Thread(target=gino_agent_executor, args=(event.source.user_id, event.message.text)).start()

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
