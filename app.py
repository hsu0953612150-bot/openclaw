import os
import threading
import time
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from openai import OpenAI
from tavily import TavilyClient

app = Flask(__name__)

# 初始化 API 金鑰 (從 Render 環境變數讀取)
line_bot_api = LineBotApi(os.getenv('LINE_CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.getenv('LINE_CHANNEL_SECRET'))
ai_client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))
tavily = TavilyClient(api_key=os.getenv('TAVILY_API_KEY'))

def gino_agent_logic(user_id, user_msg):
    try:
        # --- 階段 1: 聯網搜尋 ---
        line_bot_api.push_message(user_id, TextSendMessage(text="🌐 [大G聯網中]\n正在搜尋相關技術文件與最新資訊..."))
        search_result = tavily.search(query=user_msg, search_depth="advanced")
        
        # 整理搜尋到的參考內容
        context = "\n".join([f"- {r['title']}: {r['content']}" for r in search_result['results'][:3]])

        # --- 階段 2: 思考歷程 (基於搜尋結果) ---
        thought_prompt = f"用戶問題: {user_msg}\n搜尋參考資訊: {context}\n請列出 3 個處理此問題的邏輯步驟。"
        thought_completion = ai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "你叫做「大G」，是個具備聯網能力的開發專家。請列出思考步驟。"},
                {"role": "user", "content": thought_prompt}
            ]
        )
        thoughts = thought_completion.choices[0].message.content
        line_bot_api.push_message(user_id, TextSendMessage(text=f"🧠 [大G思考歷程]\n{thoughts}"))

        # --- 階段 3: 最終回答 (強調安全與專業) ---
        final_prompt = f"參考資訊: {context}\n問題: {user_msg}"
        final_completion = ai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "你是「大G」，由 OpenClaw 技術驅動。請根據參考資訊回答用戶，語氣要專業且親切。若涉及 API 串接，請提供代碼範例並提醒安全存放 Key。"},
                {"role": "user", "content": final_prompt}
            ]
        )
        answer = final_completion.choices[0].message.content
        line_bot_api.push_message(user_id, TextSendMessage(text=f"✅ 大G回報：\n{answer}"))

    except Exception as e:
        line_bot_api.push_message(user_id, TextSendMessage(text=f"❌ 大G模組異常: {str(e)}"))

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except:
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    # 立即回覆回條
    line_bot_api.reply_message(
        event.reply_token, 
        TextSendMessage(text="🤖 大G已接收指令，正在啟動聯網思考模組...")
    )
    # 啟動背景處理緒
    threading.Thread(target=gino_agent_logic, args=(event.source.user_id, event.message.text)).start()

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
