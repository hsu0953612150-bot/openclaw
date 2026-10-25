import json
import unittest

from gemini_errors import (
    GENERIC_REPLY,
    QUOTA_REPLY,
    GeminiQuotaError,
    is_project_quota_exhausted,
    user_facing_reply,
)


class GeminiQuotaErrorTests(unittest.TestCase):
    def test_recognizes_project_quota_exhaustion(self):
        body = json.dumps({
            "error": {
                "status": "RESOURCE_EXHAUSTED",
                "message": "You exceeded your current quota, please check your plan and billing details.",
            },
        })
        self.assertTrue(is_project_quota_exhausted(429, body))

    def test_does_not_classify_other_http_errors_as_quota_exhaustion(self):
        body = json.dumps({
            "error": {
                "status": "INVALID_ARGUMENT",
                "message": "The request is invalid.",
            },
        })
        self.assertFalse(is_project_quota_exhausted(400, body))
        self.assertFalse(is_project_quota_exhausted(503, body))
        self.assertFalse(is_project_quota_exhausted(429, "not json"))

    def test_uses_actionable_message_only_for_quota_errors(self):
        self.assertEqual(user_facing_reply(GeminiQuotaError()), QUOTA_REPLY)
        self.assertEqual(user_facing_reply(RuntimeError("unrelated")), GENERIC_REPLY)


if __name__ == "__main__":
    unittest.main()
