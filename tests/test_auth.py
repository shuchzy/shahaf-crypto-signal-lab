import hashlib
import os
import unittest
from unittest.mock import patch

from app import AuthManager


class AuthTests(unittest.TestCase):
    def test_password_and_signed_session(self):
        salt = bytes.fromhex("00112233445566778899aabbccddeeff")
        digest = hashlib.pbkdf2_hmac("sha256", b"test-password", salt, 1000)
        password_hash = f"pbkdf2_sha256$1000${salt.hex()}${digest.hex()}"
        with patch.dict(
            os.environ,
            {
                "APP_USERNAME": "tester",
                "APP_PASSWORD_HASH": password_hash,
                "SESSION_SECRET": "unit-test-secret",
            },
            clear=False,
        ):
            auth = AuthManager()
            self.assertTrue(auth.verify_password("test-password"))
            self.assertFalse(auth.verify_password("wrong"))
            self.assertTrue(auth.valid_session(auth.create_session()))


if __name__ == "__main__":
    unittest.main()
