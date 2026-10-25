>>
import json
import unittest
from unittest.mock import patch

from openai_fallback import (
    OPENAI_API_URL,
    OpenAIFallbackUnavailable,
    generate_text_fallback,
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


if __name__ == "__main__":
    unittest.main()
