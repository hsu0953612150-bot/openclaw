"""Real MP3 clips for LINE native audio, using the user's approved providers."""
import json
import logging
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request

_primary_retry_after = 0
_fallback_retry_after = 0
_generation_lock = threading.Lock()
MAX_TEXT = 1200


def speech_text(text):
    text = str(text).strip() or "已完成，沒有文字輸出。"
    if len(text) > MAX_TEXT:
        return text[:MAX_TEXT] + "。這則回答較長，語音先讀到這裡，完整內容請看文字。"
    return text


def openai_speech(text):
    key = os.getenv("OPENAI_API_KEY", "")
    if not key:
        raise RuntimeError("Existing OpenAI speech credentials are unavailable")
    body = json.dumps({"model": os.getenv("OPENAI_TTS_MODEL", "gpt-4o-mini-tts"), "voice": os.getenv("OPENAI_TTS_VOICE", "coral"), "input": speech_text(text), "instructions": "請以自然、清楚、親切的台灣繁體中文朗讀，不要讀出Markdown標記。", "response_format": "pcm"}).encode()
    request = urllib.request.Request("https://api.openai.com/v1/audio/speech", data=body, headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            pcm = response.read(24 * 1024 * 1024 + 1)
    except urllib.error.HTTPError as error:
        raise RuntimeError("OpenAI speech HTTP " + str(error.code)) from None
    if not pcm or len(pcm) > 24 * 1024 * 1024 or len(pcm) % 2:
        raise RuntimeError("Speech provider returned invalid PCM audio")
    duration = max(1, round(len(pcm) * 1000 / 48000))
    mp3 = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "s16le", "-ar", "24000", "-ac", "1", "-i", "pipe:0", "-f", "mp3", "-codec:a", "libmp3lame", "-b:a", "96k", "pipe:1"], input=pcm, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, timeout=30).stdout
    if not mp3:
        raise RuntimeError("Speech conversion returned empty MP3")
    return mp3, duration


def synthesize_native(text, primary, fallback=None):
    global _primary_retry_after, _fallback_retry_after
    fallback = fallback or openai_speech
    with _generation_lock:
        if time.time() >= _primary_retry_after:
            try:
                return primary(speech_text(text))
            except Exception:
                _primary_retry_after = time.time() + 300
                logging.getLogger(__name__).warning("Primary speech unavailable; using approved OpenAI fallback")
        if time.time() < _fallback_retry_after:
            raise RuntimeError("Both speech providers temporarily unavailable")
        try:
            return fallback(speech_text(text))
        except Exception:
            _fallback_retry_after = time.time() + 60
            raise
