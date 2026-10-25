"""Text-only OpenAI fallback used when Gemini's project quota is exhausted."""

import json
import os
import urllib.error
import urllib.request


OPENAI_API_URL = "https://api.openai.com/v1/responses"
OPENAI_FALLBACK_MODEL = os.getenv("OPENAI_FALLBACK_MODEL", "gpt-4.1-mini")


class OpenAIFallbackUnavailable(RuntimeError):
    """The fallback could not be used, usually because its API key is absent."""


def generate_text_fallback(
    history,
    instructions,
    api_key=None,
    model=None,
    knowledge_text="",
    knowledge_pdfs=None,
    user_profile=None,
):
    """Send recent text dialogue and relevant 大G memory/knowledge context to OpenAI."""
    api_key = os.getenv("OPENAI_API_KEY", "") if api_key is None else api_key
    if not api_key:
        raise OpenAIFallbackUnavailable("OPENAI_API_KEY is not configured")

    profile_facts = [
        f"{key}：{value}"
        for key, value in (user_profile or {}).items()
        if str(value).strip()
    ]
    if profile_facts:
        instructions += (
            "\n\n[大G 保存的使用者長期資料；只在問題相關時使用，不可自行擴大推論]\n"
            + "\n".join(profile_facts)
        )

    input_messages = []
    for message in history[-8:]:
        role = message.get("role")
        if role not in ("user", "assistant"):
            continue
        content = message.get("content", "")
        if not isinstance(content, str) or not content.strip():
            continue
        input_messages.append({
            "role": role,
            "content": [{"type": "input_text", "text": content}],
        })
    if not input_messages:
        raise OpenAIFallbackUnavailable("No text dialogue is available for fallback")

    current_user_message = next(
        (message for message in reversed(input_messages) if message["role"] == "user"),
        None,
    )
    if knowledge_text and current_user_message:
        current_user_message["content"].append({
            "type": "input_text",
            "text": "[大G 相關記憶與知識庫檢索資料；僅供參考]\n" + knowledge_text,
        })
    for document in knowledge_pdfs or []:
        if document.get("mime_type") != "application/pdf":
            continue
        file_data = document.get("data")
        if not current_user_message or not file_data:
            break
        current_user_message["content"].append({
            "type": "input_file",
            "filename": document.get("filename") or "大G知識庫.pdf",
            "file_data": file_data,
        })

    payload = json.dumps({
        "model": model or OPENAI_FALLBACK_MODEL,
        "instructions": instructions,
        "input": input_messages,
        "max_output_tokens": 800,
        "store": False,
    }).encode("utf-8")
    request = urllib.request.Request(
        OPENAI_API_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        # Do not include request headers or key in logs or exception text.
        raise OpenAIFallbackUnavailable(
            f"OpenAI API returned HTTP {error.code}"
        ) from error
    except (urllib.error.URLError, TimeoutError, ValueError) as error:
        raise OpenAIFallbackUnavailable(
            f"OpenAI API request failed ({type(error).__name__})"
        ) from error

    answer = "".join(
        item.get("text", "")
        for output in result.get("output", [])
        if output.get("type") == "message"
        for item in output.get("content", [])
        if item.get("type") == "output_text"
    ).strip()
    if not answer:
        raise OpenAIFallbackUnavailable("OpenAI returned no text output")
    return answer[:4900]
