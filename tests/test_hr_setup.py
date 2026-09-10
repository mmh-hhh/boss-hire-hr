import importlib.metadata
import io
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch
from scripts.run_offline_tests import supported_python
from scripts import setup_hr
from scripts.setup_hr import initialize, diagnose


class HrSetupTests(unittest.TestCase):
    def diagnose(self, root, *, environ, version):
        return diagnose(
            root,
            environ=environ,
            version=version,
            system_name="Darwin",
            python_version=(3, 13),
            in_project_venv=True,
            git_available=True,
            chrome_available=True,
        )

    def test_clean_install_is_idempotent_and_keeps_private_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(len(initialize(root)), 3)
            smoke = root / 'data/local/run_configs/smoke.json'
            self.assertEqual(
                smoke.read_text(),
                '{\n'
                '  "recommendation_source_enabled": 0,\n'
                '  "top_priority_search_query_count": 1,\n'
                '  "second_page_search_query_count": 0,\n'
                '  "recent_view_filter": "include_all"\n'
                '}\n',
            )
            (root / '.env').write_text('OPENAI_API_KEY=private-value\n')
            self.assertEqual(initialize(root), [])
            self.assertIn('private-value', (root / '.env').read_text())
            result = self.diagnose(root, environ={}, version=lambda _: '1.19.1')
            self.assertNotIn('private-value', str(result))
            self.assertFalse(result['login_verified'])
            self.assertFalse(result['local_ready'])
            self.assertEqual(result['boss_requests'], 0)

    def test_init_succeeds_when_files_are_created_before_model_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = io.StringIO()
            with patch.object(setup_hr, "ROOT", root), redirect_stdout(output):
                return_code = setup_hr.main(["init"])
            self.assertEqual(return_code, 0)
            self.assertTrue((root / ".env").is_file())
            self.assertIn("OPENAI_BASE_URL", output.getvalue())

    def test_missing_dependencies_and_empty_settings_are_actionable(self):
        def missing(_):
            raise importlib.metadata.PackageNotFoundError
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            initialize(root)
            result = self.diagnose(root, environ={}, version=missing)
            self.assertEqual(len(result['issues']), 4)
            self.assertNotIn("python3 scripts/setup_hr.py", str(result))
            ready = self.diagnose(root, environ={'OPENAI_API_KEY': 'secret', 'OPENAI_BASE_URL': 'https://example.invalid'}, version=lambda _: '1.19.1')
            self.assertTrue(ready['local_ready'])
            self.assertNotIn('secret', str(ready))

    def test_runtime_problems_are_reported_before_dependency_or_configuration_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = diagnose(
                root,
                environ={},
                version=lambda _: "1.19.1",
                system_name="Darwin",
                python_version=(3, 9),
                in_project_venv=False,
                git_available=True,
                chrome_available=False,
            )
            self.assertFalse(result["local_ready"])
            self.assertEqual(result["python_version"], "3.9")
            self.assertIn("Python 3.11", result["next_action"])
            self.assertNotIn(str(root), str(result))

    def test_base_url_must_be_service_root_without_v1(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            initialize(root)
            result = self.diagnose(
                root,
                environ={
                    "OPENAI_API_KEY": "secret",
                    "OPENAI_BASE_URL": "https://example.invalid/v1",
                },
                version=lambda _: "1.19.1",
            )
            self.assertFalse(result["local_ready"])
            self.assertTrue(any("/v1" in issue for issue in result["issues"]))
            self.assertIn("本人编辑 .env", result["next_action"])

    def test_offline_test_runner_rejects_old_python_before_discovery(self):
        self.assertFalse(supported_python((3, 9)))
        self.assertTrue(supported_python((3, 11)))

    def test_native_windows_is_not_reported_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            initialize(root)
            result = diagnose(
                root,
                environ={"OPENAI_API_KEY": "fake", "OPENAI_BASE_URL": "https://example.invalid"},
                version=lambda _: "1.19.1",
                system_name="Windows",
                python_version=(3, 13),
                in_project_venv=True,
                git_available=True,
                chrome_available=True,
            )
            self.assertFalse(result["local_ready"])
            self.assertTrue(any("Windows" in issue for issue in result["issues"]))
