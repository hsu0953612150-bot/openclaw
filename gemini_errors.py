import json


class GeminiQuotaError(RuntimeError):
    """Gemini rejected a request because the project quota is exhausted."""


QUOTA_REPLY = (
    "大G目前的 Gemini API 額度或呼叫頻率已達上限，暫時無法產生回覆。"
    "請稍後再試；若持續發生，請管理者檢查 Google AI Studio 的用量與計費設定。"
)

GENERIC_REPLY = "我目前暫時無法完成這則訊息，請稍後再試。系統已記錄錯誤供檢查。"


def is_project_quota_exhausted(status_code, response_body):
    if status_code != 429:
        return False

    try:
        error = json.loads(response_body).get("error", {})
    except (TypeError, ValueError):
        return False

    if not isinstance(error, dict):
        return False

    status = str(error.get("status", "")).upper()
    message = str(error.get("message", "")).casefold()
    return status == "RESOURCE_EXHAUSTED" and (
        "exceeded your current quota" in message
        or "check your plan and billing details" in message
    )


def user_facing_reply(error):
    if isinstance(error, GeminiQuotaError):
        return QUOTA_REPLY
    return GENERIC_REPLY
