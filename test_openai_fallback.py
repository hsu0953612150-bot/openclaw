import json
import unittest
from unittest.mock import patch

from openai_fallback import (
    OPENAI_API_URL,
    OpenAIFallbackUnavailable,
    generate_text_fallback,
    transcribe_audio_fallback,
)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class OpenAIFallbackTests(unittest.TestCase):
    @patch("openai_fallback.urllib.request.urlopen")
    def test_transcribes_line_audio_with_gpt_transcribe_and_traditional_chinese_hint(self, urlopen):
        urlopen.return_value = FakeResponse({"text": "請幫我整理資料"})
        transcript = transcribe_audio_fallback("YXVkaW8=", "audio/m4a", api_key="test-key")
        self.assertEqual(transcript, "請幫我整理資料")
        request = urlopen.call_args.args[0]
        self.assertIn("/audio/transcriptions", request.full_url)
        self.assertEqual(request.get_header("Authorization"), "Bearer test-key")
        self.assertIn(b'name="model"\r\n\r\ngpt-transcribe', request.data)
        self.assertIn(b'name="languages[]"\r\n\r\nzh-tw', request.data)
        self.assertIn(b'filename="voice.m4a"', request.data)
        self.assertIn(b'audio', request.data)

    def test_rejects_unsupported_audio_without_api_call(self):
        with self.assertRaises(OpenAIFallbackUnavailable):
            transcribe_audio_fallback("YXVkaW8=", "application/octet-stream", api_key="test-key")

    @patch("openai_fallback.urllib.request.urlopen")
    def test_web_search_returns_source_citations(self, urlopen):
        urlopen.return_value = FakeResponse({"output": [{"type": "message", "content": [{
            "type": "output_text", "text": "研究答案", "annotations": [{
                "type": "url_citation", "url_citation": {"url": "https://example.com/source", "title": "來源"},
            }],
        }]}]})
        result = generate_text_fallback(
            [{"role": "user", "content": "近期問題"}], "rules", api_key="test-key",
            use_web_search=True, return_metadata=True,
        )
        payload = json.loads(urlopen.call_args.args[0].data.decode("utf-8"))
        self.assertEqual(payload["tools"], [{"type": "web_search"}])
        self.assertEqual(result["answer"], "研究答案")
        self.assertEqual(result["citations"][0]["url"], "https://example.com/source")

    @patch("openai_fallback.urllib.request.urlopen")
    def test_sends_only_recent_plain_text_dialogue(self, urlopen):
        urlopen.return_value = FakeResponse({
            "output": [{"type": "message", "content": [
                {"type": "output_text", "text": "GPT 備援回答"},
            ]}],
        })
        history = [
            {"role": "user", "content": f"舊訊息 {index}"}
            for index in range(10)
        ] + [
            {"role": "assistant", "content": "最近回覆"},
            {"role": "user", "content": "目前問題"},
        ]

        answer = generate_text_fallback(
            history,
            "system instruction",
            api_key="test-key",
        )

        self.assertEqual(answer, "GPT 備援回答")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, OPENAI_API_URL)
        self.assertEqual(request.get_header("Authorization"), "Bearer test-key")
        payload = json.loads(request.data.decode("utf-8"))
        sent_text = [
            item["text"]
            for message in payload["input"]
            for item in message["content"]
        ]
        self.assertEqual(sent_text, [
            "舊訊息 4", "舊訊息 5", "舊訊息 6", "舊訊息 7",
            "舊訊息 8", "舊訊息 9", "最近回覆", "目前問題",
        ])
        self.assertFalse(payload["store"])
        self.assertEqual(payload["instructions"], "system instruction")
        # A multi-turn request containing assistant input_text is rejected (HTTP 400).
        self.assertEqual(payload["input"][-2]["role"], "assistant")
        self.assertEqual(payload["input"][-2]["content"][0]["type"], "output_text")
        self.assertEqual(payload["input"][-1]["content"][0]["type"], "input_text")

    def test_requires_openai_api_key(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(OpenAIFallbackUnavailable):
                generate_text_fallback([{"role": "user", "content": "hi"}], "rules")

    @patch("openai_fallback.urllib.request.urlopen")
    def test_rejects_empty_model_output(self, urlopen):
        urlopen.return_value = FakeResponse({"output": []})
        with self.assertRaises(OpenAIFallbackUnavailable):
            generate_text_fallback(
                [{"role": "user", "content": "hi"}],
                "rules",
                api_key="test-key",
            )

    @patch("openai_fallback.urllib.request.urlopen")
    def test_includes_relevant_daga_context_and_profile(self, urlopen):
        urlopen.return_value = FakeResponse({
            "output": [{"type": "message", "content": [
                {"type": "output_text", "text": "已參考大G資料"},
            ]}],
        })
        generate_text_fallback(
            [{"role": "user", "content": "我的資料有哪些？"}],
            "system instruction",
            api_key="test-key",
            knowledge_text="本次檢索到的相關記錄",
            knowledge_pdfs=[{
                "mime_type": "application/pdf",
                "filename": "reference.pdf",
                "data": "cGRmLWJhc2U2NA==",
            }],
            user_profile={"常用語言": "繁體中文"},
        )

        payload = json.loads(urlopen.call_args.args[0].data.decode("utf-8"))
        self.assertIn("常用語言：繁體中文", payload["instructions"])
        content = payload["input"][-1]["content"]
        self.assertEqual(content[1]["type"], "input_text")
        self.assertIn("本次檢索到的相關記錄", content[1]["text"])
        self.assertEqual(content[2]["type"], "input_file")
        self.assertEqual(content[2]["filename"], "reference.pdf")
        self.assertEqual(content[2]["file_data"], "data:application/pdf;base64,cGRmLWJhc2U2NA==")


if __name__ == "__main__":
    unittest.main()
