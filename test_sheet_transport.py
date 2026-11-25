import base64
import json
import os
import unittest
import urllib.parse
from unittest.mock import Mock, patch

from video_knowledge import Cloud
from codex_line_bridge import BridgeStore

SHEETS = "https://www.googleapis.com/auth/spreadsheets"


class SheetTransportTests(unittest.TestCase):
    @patch.dict(os.environ, {"GOOGLE_OAUTH_REFRESH_TOKEN": "expired-personal-token"})
    def test_queue_auth_uses_existing_service_account_without_refreshing_personal_grant(self):
        cloud = Cloud(auth_mode="service_account", scopes=[SHEETS])
        cloud.credentials = {"client_email": "existing@example.invalid", "private_key": "test-key"}
        response = Mock()
        response.read.return_value = json.dumps({"access_token": "test-access", "expires_in": 3600}).encode()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        key = Mock()
        key.sign.return_value = b"signature"
        with patch("cryptography.hazmat.primitives.serialization.load_pem_private_key", return_value=key), patch("urllib.request.urlopen", return_value=response) as request:
            self.assertEqual(cloud.token(), "test-access")
            self.assertEqual(cloud.token(), "test-access")
        request.assert_called_once()
        form = urllib.parse.parse_qs(request.call_args.args[0].data.decode())
        self.assertNotIn("refresh_token", form)
        self.assertNotIn("expired-personal-token", str(form))
        claims = form["assertion"][0].split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(claims + "=" * (-len(claims) % 4)))
        self.assertEqual(claims["scope"], SHEETS)

    def test_queue_and_personal_drive_keep_separate_credentials_and_scopes(self):
        store = BridgeStore()
        self.assertEqual(store.cloud.auth_mode, "service_account")
        self.assertEqual(store.cloud.scopes, [SHEETS])
        self.assertEqual(store.drive_cloud.auth_mode, "service_account")
        self.assertEqual(store.drive_cloud.scopes, ["https://www.googleapis.com/auth/drive.readonly"])
        self.assertIsNot(store.cloud, store.drive_cloud)

    def test_explicit_test_or_readonly_cloud_dependency_is_preserved(self):
        cloud = Mock(sheet_id="existing-sheet")
        store = BridgeStore(cloud)
        self.assertIs(store.cloud, cloud)
        self.assertIs(store.drive_cloud, cloud)


if __name__ == "__main__":
    unittest.main()

