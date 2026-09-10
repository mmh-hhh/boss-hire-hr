from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from boss_agent_cli.auth.token_store import TokenStore

from boss_hire.boss_auth_sync import load_saved_session_fingerprint, sync_auth_from_chrome


class BossAuthSyncTests(unittest.TestCase):
    def test_sync_backs_up_stale_auth_and_persists_current_chrome_cookies(self) -> None:
        old = {"cookies": {"wt2": "old-wt2", "zp_at": "old-zp-at"}, "stoken": "", "user_agent": ""}
        current = {
            "cookies": {"wt2": "current-wt2", "zp_at": "current-zp-at", "wbg": "1"},
            "stoken": "",
            "user_agent": "",
        }
        with tempfile.TemporaryDirectory() as tmp:
            auth_dir = Path(tmp) / "auth-data"
            store = TokenStore(auth_dir / "auth")
            store.save(old)

            result = sync_auth_from_chrome(
                auth_dir,
                extractor=lambda *_args, **_kwargs: current,
                input_fn=lambda _: "确认仍为本人原账号",
            )

            self.assertTrue(result["changed"])
            self.assertEqual(result["cookie_count"], 3)
            self.assertEqual(result["session_fingerprint"], load_saved_session_fingerprint(auth_dir))
            self.assertEqual(store.load(), current)
            backup = auth_dir / "auth" / "session.enc.pre-chrome-sync"
            self.assertTrue(backup.is_file())
            self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
            self.assertNotIn("current-wt2", str(result))
            self.assertNotIn("current-zp-at", str(result))

    def test_sync_fails_without_current_primary_cookie_and_preserves_saved_auth(self) -> None:
        old = {"cookies": {"wt2": "old-wt2"}, "stoken": "", "user_agent": ""}
        with tempfile.TemporaryDirectory() as tmp:
            auth_dir = Path(tmp) / "auth-data"
            store = TokenStore(auth_dir / "auth")
            store.save(old)

            with self.assertRaisesRegex(RuntimeError, "Chrome.*wt2"):
                sync_auth_from_chrome(
                    auth_dir,
                    extractor=lambda *_args, **_kwargs: {"cookies": {}},
                )

            self.assertEqual(store.load(), old)


if __name__ == "__main__":
    unittest.main()

class HrAuthDiagnosticsTests(unittest.TestCase):
    def test_extractor_error_is_redacted_and_saved_state_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            auth_dir = Path(tmp)
            store = TokenStore(auth_dir / 'auth')
            old = {'cookies': {'wt2': 'old'}}
            store.save(old)
            def fail(*args, **kwargs):
                raise OSError('secret-cookie-value')
            with self.assertRaisesRegex(RuntimeError, '提取失败') as error:
                sync_auth_from_chrome(auth_dir, extractor=fail)
            self.assertNotIn('secret-cookie-value', str(error.exception))
            self.assertEqual(store.load(), old)

    def test_changed_session_requires_exact_human_identity_check_before_save(self):
        with tempfile.TemporaryDirectory() as tmp:
            auth_dir = Path(tmp)
            store = TokenStore(auth_dir / 'auth')
            old = {'cookies': {'wt2': 'old'}}
            store.save(old)
            with self.assertRaisesRegex(RuntimeError, '会话变化'):
                sync_auth_from_chrome(auth_dir, extractor=lambda *a, **kw: {'cookies': {'wt2': 'new'}}, input_fn=lambda _: 'yes')
            self.assertEqual(store.load(), old)
            self.assertFalse((auth_dir / 'auth/session.enc.pre-chrome-sync').exists())

    def test_unchanged_primary_cookie_does_not_ask_for_identity_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            auth_dir = Path(tmp)
            TokenStore(auth_dir / 'auth').save({'cookies': {'wt2': 'same'}})
            result = sync_auth_from_chrome(auth_dir, extractor=lambda *a, **kw: {'cookies': {'wt2': 'same'}, 'stoken': 'fresh'}, input_fn=lambda _: self.fail('unchanged primary session'))
            self.assertFalse(result['session_changed'])
