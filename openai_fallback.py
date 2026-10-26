"""Text-only OpenAI fallback used when Gemini's project quota is exhausted."""

import base64
import json
import os
import urllib.error
import urllib.request
import uuid


OPENAI_API_URL = "https://api.openai.com/v1/responses"
OPENAI_FALLBACK_MODEL = os.getenv("OPENAI_FALLBACK_MODEL", "gpt-4.1-mini")
OPENAI_REALTIME_MODEL = os.getenv("OPENAI_REALTIME_MODEL", "gpt-realtime-mini")
OPENAI_TRANSCRIPTIONS_URL = "https://api.openai.com/v1/audio/transcriptions"
OPENAI_TRANSCRIPTION_MODEL = os.getenv("OPENAI_TRANSCRIPTION_MODEL", "gpt-transcribe")


class OpenAIFallbackUnavailable(RuntimeError):
    """The fallback could not be used, usually because its API key is absent."""


class OpenAITranscriptionUnavailable(OpenAIFallbackUnavailable):
    """The LINE voice message could not be transcribed by OpenAI."""


def transcribe_audio_fallback(audio_base64, mime_type="audio/m4a", api_key=None, model=None):
    """Transcribe one received LINE audio message without storing the audio locally."""
    api_key = os.getenv("OPENAI_API_KEY", "") if api_key is None else api_key
    if not api_key:
        raise OpenAITranscriptionUnavailable("OPENAI_API_KEY is not configured")
    try:
        audio = base64.b64decode(audio_base64, validate=True)
    except (ValueError, TypeError) as error:
        raise OpenAITranscriptionUnavailable("LINE audio payload is invalid") from error
    if not audio or len(audio) > 25 * 1024 * 1024:
        raise OpenAITranscriptionUnavailable("LINE audio is empty or exceeds 25 MB")

    format_info = {
        "audio/m4a": ("voice.m4a", "audio/mp4"),
        "audio/mp4": ("voice.m4a", "audio/mp4"),
        "audio/mpeg": ("voice.mp3", "audio/mpeg"),
        "audio/mp3": ("voice.mp3", "audio/mpeg"),
        "audio/wav": ("voice.wav", "audio/wav"),
        "audio/x-wav": ("voice.wav", "audio/wav"),
        "audio/ogg": ("voice.ogg", "audio/ogg"),
        "audio/webm": ("voice.webm", "audio/webm"),
    }
    try:
        filename, file_mime = format_info[(mime_type or "").split(";", 1)[0].lower()]
    except KeyError as error:
        raise OpenAITranscriptionUnavailable("LINE audio format is not supported") from error

    boundary = "daga_transcription_" + uuid.uuid4().hex
    fields = [
        ("model", model or OPENAI_TRANSCRIPTION_MODEL, None, None),
        ("languages[]", "zh-tw", None, None),
        ("response_format", "json", None, None),
        ("file", audio, filename, file_mime),
    ]
    chunks = []
    for name, value, file_name, content_type in fields:
        chunks.append(f"--{boundary}\r\n".encode("ascii"))
        disposition = f'Content-Disposition: form-data; name="{name}"'
        if file_name:
            disposition += f'; filename="{file_name}"'
        chunks.append((disposition + "\r\n").encode("ascii"))
        if content_type:
            chunks.append((f"Content-Type: {content_type}\r\n").encode("ascii"))
        chunks.append(b"\r\n")
        chunks.append(value if isinstance(value, bytes) else value.encode("utf-8"))
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode("ascii"))
    request = urllib.request.Request(
        OPENAI_TRANSCRIPTIONS_URL,
        data=b"".join(chunks),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise OpenAITranscriptionUnavailable(
            f"OpenAI transcription API returned HTTP {error.code}"
        ) from error
    except (urllib.error.URLError, TimeoutError, ValueError) as error:
        raise OpenAITranscriptionUnavailable(
            f"OpenAI transcription request failed ({type(error).__name__})"
        ) from error
    transcript = result.get("text", "").strip()
    if not transcript:
        raise OpenAITranscriptionUnavailable("OpenAI returned an empty transcript")
    return transcript[:12000]


def create_realtime_client_secret(api_key=None, model=None, instructions=""):
    """Create a short-lived browser secret for a Realtime WebRTC voice session."""
    api_key = os.getenv("OPENAI_API_KEY", "") if api_key is None else api_key
    if not api_key:
        raise OpenAIFallbackUnavailable("OPENAI_API_KEY is not configured")

    payload = json.dumps({
        "expires_after": {"anchor": "created_at", "seconds": 600},
        "session": {
            "type": "realtime",
            "model": model or OPENAI_REALTIME_MODEL,
            "instructions": instructions,
            "output_modalities": ["audio"],
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": 24000},
                    "transcription": {"model": "gpt-4o-mini-transcribe", "language": "zh"},
                    "turn_detection": {"type": "server_vad"},
                },
                "output": {
                    "format": {"type": "audio/pcm", "rate": 24000},
                    "voice": "marin",
                },
            },
            "max_output_tokens": 800,
        },
    }).encode("utf-8")
    request = urllib.request.Request(
        "https://api.openai.com/v1/realtime/client_secrets",
        data=payload,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise OpenAIFallbackUnavailable(f"OpenAI Realtime API returned HTTP {error.code}") from error
    except (urllib.error.URLError, TimeoutError, ValueError) as error:
        raise OpenAIFallbackUnavailable(
            f"OpenAI Realtime API request failed ({type(error).__name__})"
        ) from error

    value = result.get("value")
    if not value:
        raise OpenAIFallbackUnavailable("OpenAI returned no Realtime client secret")
    return value, (result.get("session") or {}).get("model") or model or OPENAI_REALTIME_MODEL


def generate_text_fallback(
    history,
    instructions,
    api_key=None,
    model=None,
    knowledge_text="",
    knowledge_pdfs=None,
    user_profile=None,
    use_web_search=False,
    return_metadata=False,
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
            # Responses treats prior assistant replies as output content.
            "content": [{"type": "output_text" if role == "assistant" else "input_text", "text": content}],
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
        if not file_data.startswith("data:"):
            file_data = "data:application/pdf;base64," + file_data
        current_user_message["content"].append({
            "type": "input_file",
            "filename": document.get("filename") or document.get("name") or "大G知識庫.pdf",
            "file_data": file_data,
        })

    payload_body = {
        "model": model or OPENAI_FALLBACK_MODEL,
        "instructions": instructions,
        "input": input_messages,
        "max_output_tokens": 800,
        "store": False,
    }
    if use_web_search:
        payload_body["tools"] = [{"type": "web_search"}]
    payload = json.dumps(payload_body).encode("utf-8")
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

    answer_parts = []
    citations = []
    for output in result.get("output", []):
        if output.get("type") != "message":
            continue
        for item in output.get("content", []):
            if item.get("type") != "output_text":
                continue
            if item.get("text"):
                answer_parts.append(item["text"])
            for annotation in item.get("annotations", []):
                if annotation.get("type") != "url_citation":
                    continue
                citation = annotation.get("url_citation") or {}
                url = citation.get("url", "")
                if url.startswith("https://") and all(existing["url"] != url for existing in citations):
                    citations.append({"url": url, "title": citation.get("title", "")[:300]})
    answer = "".join(answer_parts).strip()
    if not answer:
        raise OpenAIFallbackUnavailable("OpenAI returned no text output")
    if return_metadata:
        return {"answer": answer[:4300], "citations": citations[:8]}
    return answer[:4900]
