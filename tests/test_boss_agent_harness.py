from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HOOK_PATH = ROOT / ".codex/hooks/boss_access_policy.py"


def load_hook_module():  # noqa: ANN201
    spec = importlib.util.spec_from_file_location("boss_access_policy", HOOK_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load BOSS access hook")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BossAgentHarnessTests(unittest.TestCase):
    def test_pre_tool_hook_blocks_browser_and_computer_tools_in_this_repo(self) -> None:
        hook = load_hook_module()
        for tool_name in (
            "mcp__chrome__click",
            "mcp__browser__navigate",
            "mcp__computer_use__computer",
            "playwright_cli",
        ):
            with self.subTest(tool_name=tool_name):
                reason = hook.block_reason(
                    {"cwd": str(ROOT), "tool_name": tool_name, "tool_input": {}}
                )
                self.assertIn("browser", reason.lower())

    def test_pre_tool_hook_blocks_live_launcher_raw_cli_and_boss_http_bypasses(self) -> None:
        hook = load_hook_module()
        blocked_commands = (
            "python3 scripts/run_single_job_live.py favorite-close --note stop",
            "python3 scripts/run_single_job_live.py jobs --refresh",
            "python3 scripts/run_single_job_live.py jobs --select 2",
            "python3 scripts/run_single_job_live.py prepare-job",
            ".venv/bin/python scripts/run_single_job_live.py start --config search_top5",
            "python scripts/run_single_job_live.py continue",
            "python3 scripts/run_single_job_live.py close --note done",
            ".venv/bin/python scripts/run_single_job_live.py run --live --authorization-id x",
            ".venv/bin/boss recruiter search",
            "python -m boss_agent_cli recruiter search",
            "curl https://www.zhipin.com/wapi/zpjob/job/list.json",
            "npx playwright open https://www.zhipin.com",
        )
        for command in blocked_commands:
            with self.subTest(command=command):
                reason = hook.block_reason(
                    {"cwd": str(ROOT), "tool_name": "Bash", "tool_input": {"command": command}}
                )
                self.assertTrue(reason)

    def test_pre_tool_hook_allows_offline_tests_and_non_browser_local_work(self) -> None:
        hook = load_hook_module()
        for command in (
            ".venv/bin/python scripts/run_offline_tests.py",
            "python3 scripts/setup_hr.py check",
            "python3 scripts/setup_hr.py init",
            ".venv/bin/python -m unittest tests.test_boss_guard -v",
            ".venv/bin/python scripts/run_single_job_live.py configs",
            "python3 scripts/run_single_job_live.py configs --json",
            "python scripts/run_single_job_live.py status",
            "python scripts/run_single_job_live.py status --run run-1 --json",
            "rg -n zhipin.com tests boss_hire",
        ):
            with self.subTest(command=command):
                self.assertIsNone(
                    hook.block_reason(
                        {"cwd": str(ROOT), "tool_name": "Bash", "tool_input": {"command": command}}
                    )
                )

    def test_codex_hook_and_rules_are_wired_and_agent_instructions_are_short(self) -> None:
        hook_config = ROOT / ".codex/hooks.json"
        self.assertTrue(hook_config.is_relative_to(ROOT))
        hooks = json.loads(hook_config.read_text(encoding="utf-8"))
        pre_tool = hooks["hooks"]["PreToolUse"]
        self.assertEqual(pre_tool[0]["matcher"], "*")
        hook_command = pre_tool[0]["hooks"][0]["command"]
        self.assertIn("boss_access_policy.py", hook_command)
        self.assertNotIn("~/.codex", hook_command)
        self.assertNotIn(str(Path.home() / ".codex"), hook_command)
        self.assertNotIn("dangerously-bypass-hook-trust", hook_command)
        self.assertEqual(pre_tool[0]["hooks"][0]["type"], "command")
        self.assertEqual(pre_tool[0]["hooks"][0]["timeout"], 5)

        rules = (ROOT / ".codex/rules/boss.rules").read_text(encoding="utf-8")
        self.assertIn('decision = "forbidden"', rules)
        self.assertIn("run_single_job_live.py", rules)
        self.assertIn("boss_agent_cli", rules)
        self.assertIn("playwright", rules)

        agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("human-operated", agents)
        self.assertIn("scripts/run_offline_tests.py", agents)
        self.assertIn("strictly serial", agents)
        self.assertIn("recommendation_source_enabled", agents)
        self.assertIn("top_priority_search_query_count", agents)
        self.assertIn("second_page_search_query_count", agents)
        self.assertIn("recent_view_filter", agents)
        self.assertIn("viewResume=1", agents)
        self.assertIn("scripts/search_filters.py list", agents)
        self.assertIn("search_filter_params", agents)
        self.assertIn("popular cities", agents)
        self.assertIn("filterParams.region", agents)
        self.assertIn("single-use detail manifest", agents)
        self.assertIn("configs` and `status` are local-only", agents)
        self.assertIn("One invocation performs at most one external stage", agents)
        self.assertNotIn("exactly one candidate-source list request", agents)
        self.assertIn("never follow `hasMore`", agents)
        self.assertIn("only list flow allowed beyond search page 2", agents)
        self.assertIn("fixed `tag=4`", agents)
        self.assertIn("capped at 40 pages", agents)
        self.assertIn("separate human runs and authorizations", agents)
        self.assertIn("stable `encryptGeekId` values only", agents)
        self.assertIn("recheck the registry under lock", agents)
        self.assertLess(len(agents.splitlines()), 40)


if __name__ == "__main__":
    unittest.main()
