"""Classify before queueing; mentioning a computer is not an instruction to use it."""
import re
import time

CONTROL = r"(?:綁定電腦|绑定电脑)(?:\s+\S+)?|(?:電腦狀態|电脑状态|停止任務|停止任务)|(?:確認|确认|拒絕|拒绝|回答)\s+[A-Fa-f0-9]{8}(?:\s+[\s\S]+)?"


def is_control(text):
    return bool(re.fullmatch(CONTROL, text.strip()))


def needs_computer(text, active=False):
    text = text.strip()
    if is_control(text):
        return True
    if re.search(r"^(?:請問|请问)?(?:如何|怎麼|怎么|什麼是|什么是|教我)|(?:是什麼|是什么|有什麼差別|有什么区别)[？?。！!]*$", text):
        return False
    if re.search(r"(?:不要|不用|別|别|無需|无需).{0,6}(?:操作|使用|打開|打开|開啟|开启).{0,12}(?:電腦|电脑|Chrome|Edge|瀏覽器|浏览器|YouTube)", text, re.I):
        return False
    if re.search(r"(?:交給|交给|請用|请用|透過|通过|使用).{0,8}(?:Codex|電腦|电脑).{0,15}(?:處理|处理|執行|执行|操作)|^(?:Codex|電腦|电脑)\s*[:：]", text, re.I):
        return True
    action = r"打開|打开|開啟|开启|關閉|关闭|啟動|启动|操作|登入|登录|點擊|点击|安裝|安装|卸載|卸载|截圖|截图|刪除|删除|移動|移动|重新命名|修改|修正|建立|創建|创建|讀取|读取|檢查|检查|儲存|储存|保存|寫入|写入|整理|清理|搜尋|搜索|找出"
    target = r"Chrome|Edge|YouTube|瀏覽器|浏览器|電腦|电脑|桌面|視窗|窗口|分頁|分页|檔案|文件|資料夾|文件夾|文件夹|本機|本机|[A-Z]:[/\\]|程式碼|代码|程式|代碼|網站|网站|網頁|网页|試算表|Excel|Word"
    if re.search(r"(?:" + action + r").{0,32}(?:" + target + r")|(?:" + target + r").{0,12}(?:" + action + r")", text, re.I):
        return True
    if re.search(r"(?:部署|除錯|调试|debug|修復程式|修复程序|寫.{0,8}程式|写.{0,8}程序)", text, re.I):
        return True
    if re.search(r"(?:這台|这台|我的)(?:電腦|电脑).{0,12}(?:狀態|状态|記憶體|内存|磁碟|磁盘|效能|性能)", text):
        return True
    return bool(active and re.fullmatch(r"(?:請|请)?(?:繼續|继续|確認一下|确认一下|再試一次|再试一次|重試|重试|好|好的|可以|選第.{1,3}個|选第.{1,3}个)[。.!！?？]*", text))


def route_classified_text(user_id, text, message_id, source_type="user", store=None):
    from codex_line_bridge import BridgeStore, route_text
    if source_type != "user":
        return None
    possible_followup = bool(re.fullmatch(r"(?:請|请)?(?:繼續|继续|確認一下|确认一下|再試一次|再试一次|重試|重试|好|好的|可以|選第.{1,3}個|选第.{1,3}个)[。.!！?？]*", text.strip()))
    if not needs_computer(text) and not possible_followup:
        return None
    store = store or BridgeStore()
    config = store.control()
    if is_control(text):
        return route_text(user_id, text, message_id, source_type, store)
    if config.get("enabled") != "true" or config.get("owner_id") != user_id:
        return None
    active = bool(config.get("active_job"))
    if possible_followup and not active:
        active = any(row[2] == user_id and row[4] == "pending" and time.time() - float(row[6] or 0) <= 1800 for _, row in store.questions())
    if needs_computer(text, active):
        return route_text(user_id, text, message_id, source_type, store)
    return None
