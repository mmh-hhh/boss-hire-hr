#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import sys
from typing import Any


BROWSER_TOOL_MARKERS = ("browser", "chrome", "computer_use", "computer-use", "playwright")
BROWSER_COMMAND = re.compile(r"(^|[\s/])(playwright|chromium|google-chrome)([\s/]|$)", re.IGNORECASE)
RAW_BOSS_CLI = re.compile(
    r"(^|\s)(?:\.venv/bin/)?boss(?:\s|$)|"
    r"(^|\s)(?:python|python3|\.venv/bin/python)\s+-m\s+boss_agent_cli(?:\s|$)|"
    r"boss-agent-cli",
    re.IGNORECASE,
)
NETWORK_COMMAND = re.compile(r"(^|\s)(curl|wget|http|xh)(\s|$)", re.IGNORECASE)
LOCAL_LAUNCHER_COMMAND = re.compile(
    r"^\s*(?:\S*/)?python(?:3(?:\.\d+)?)?\s+scripts/run_single_job_live\.py\s+"
    r"(?:configs(?:\s+--json)?|status(?:\s+(?:--json|--run\s+[A-Za-z0-9._-]+))*)\s*$"
)


def block_reason(event: dict[str, Any]) -> str | None:
    tool_name = str(event.get("tool_name") or "").lower()
    if any(marker in tool_name for marker in BROWSER_TOOL_MARKERS):
        return "BOSS repository policy: browser and computer automation are disabled; use frozen fixtures."
    if tool_name != "bash":
        return None

    tool_input = event.get("tool_input")
    command = str(tool_input.get("command") or "") if isinstance(tool_input, dict) else ""
    lowered = command.lower()
    if "scripts/run_single_job_live.py" in lowered:
        if LOCAL_LAUNCHER_COMMAND.fullmatch(command):
            return None
        return "BOSS repository policy: the live launcher is human-operated and cannot run from a coding agent."
    if RAW_BOSS_CLI.search(command):
        return "BOSS repository policy: raw boss-agent-cli access is disabled; use offline tests."
    if "run_supply_mvp.py" in lowered and "--live" in lowered:
        return "BOSS repository policy: supply MVP live mode was removed; use frozen replay."
    if BROWSER_COMMAND.search(command) or "npx playwright" in lowered:
        return "BOSS repository policy: browser automation is disabled in this repository."
    if ("zhipin.com" in lowered or "boss直聘" in lowered) and NETWORK_COMMAND.search(command):
        return "BOSS repository policy: direct BOSS HTTP commands are disabled; use frozen fixtures."
    return None


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"invalid BOSS policy hook input: {exc}", file=sys.stderr)
        return 2
    reason = block_reason(event) if isinstance(event, dict) else "invalid BOSS policy hook event"
    if reason:
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": reason,
                    }
                }
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
