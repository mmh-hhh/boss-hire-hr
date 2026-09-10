#!/usr/bin/env python3
"""Local-only onboarding; deliberately imports neither the launcher nor any client."""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import platform
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]


def initialize(root: Path) -> list[str]:
    files = {
        root / '.env': '# 请本人配置，不要将密钥发给助手或提交 Git\nOPENAI_BASE_URL=\nOPENAI_API_KEY=\nOPENAI_MODEL=gpt-5.4-mini\nBOSS_HIRE_LLM_WORKERS=4\n',
        root / 'data/local/run_configs/default.json': json.dumps({
            'recommendation_source_enabled': 0, 'top_priority_search_query_count': 3,
            'second_page_search_query_count': 0, 'recent_view_filter': 'include_all',
        }, indent=2) + '\n',
        root / 'data/local/run_configs/smoke.json': json.dumps({
            'recommendation_source_enabled': 0, 'top_priority_search_query_count': 1,
            'second_page_search_query_count': 0, 'recent_view_filter': 'include_all',
        }, indent=2) + '\n',
    }
    created = []
    for path, body in files.items():
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(fd, 'w') as stream:
            stream.write(body)
        created.append(str(path.relative_to(root)))
    return created


def _running_in_project_venv(root: Path) -> bool:
    try:
        Path(sys.executable).absolute().relative_to((root / ".venv").absolute())
    except ValueError:
        return False
    return sys.prefix != sys.base_prefix


def _chrome_available() -> bool:
    return any(
        path.exists()
        for path in (
            Path("/Applications/Google Chrome.app"),
            Path.home() / "Applications/Google Chrome.app",
        )
    )


def diagnose(
    root: Path,
    *,
    environ=None,
    version=importlib.metadata.version,
    system_name=None,
    python_version=None,
    in_project_venv=None,
    git_available=None,
    chrome_available=None,
) -> dict:
    settings = {}
    env_file = root / '.env'
    if env_file.is_file():
        for line in env_file.read_text().splitlines():
            if line.strip() and not line.lstrip().startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                settings[key.strip()] = value.strip()
    settings.update(os.environ if environ is None else environ)
    issues = []
    system_name = platform.system() if system_name is None else system_name
    python_version = tuple(sys.version_info[:2]) if python_version is None else tuple(python_version[:2])
    in_project_venv = _running_in_project_venv(root) if in_project_venv is None else in_project_venv
    git_available = shutil.which("git") is not None if git_available is None else git_available
    chrome_available = _chrome_available() if chrome_available is None else chrome_available
    supported_platform = system_name == "Darwin"
    if system_name == "Windows":
        issues.append("当前 70 分试点仅支持 macOS，原生 Windows 暂不支持；勿尝试复制他人认证。")
    elif not supported_platform:
        issues.append(f"当前 70 分试点仅支持 macOS；{system_name} 尚未完成真实登录验收。")
    if not git_available:
        issues.append("缺少 Git；请先安装 Git，再克隆分发仓库。")
    if python_version < (3, 11):
        issues.append('需要 Python 3.11 或更高版本。')
    if not in_project_venv:
        issues.append("当前未使用本仓库 .venv；请用 Python 3.11 或更高版本创建并使用 .venv。")
    if supported_platform and not chrome_available:
        issues.append("未找到 Google Chrome；请先安装 Chrome，并由本人登录 BOSS 招聘方页面。")
    for package, expected in [('boss-agent-cli', '1.19.1'), ('patchright', None)]:
        try:
            installed = version(package)
            if expected and installed != expected:
                issues.append(f'{package} 版本不匹配；请安装 requirements-boss.txt。')
        except importlib.metadata.PackageNotFoundError:
            issues.append(f'缺少 {package}；请在 .venv 安装 requirements-boss.txt。')
    for key in ('OPENAI_BASE_URL', 'OPENAI_API_KEY'):
        if not settings.get(key, '').strip():
            issues.append(f'缺少 {key}；请本人编辑 .env。')
    base_url = settings.get("OPENAI_BASE_URL", "").strip().rstrip("/")
    if base_url and not base_url.startswith(("https://", "http://")):
        issues.append("OPENAI_BASE_URL 必须是 http:// 或 https:// 开头的服务根地址。")
    elif base_url.endswith("/v1"):
        issues.append("OPENAI_BASE_URL 请填写服务根地址，不要包含末尾 /v1；程序会自动追加。")
    try:
        workers = int(settings.get('BOSS_HIRE_LLM_WORKERS', '4'))
        if not 1 <= workers <= 8:
            raise ValueError
    except ValueError:
        issues.append('BOSS_HIRE_LLM_WORKERS 必须为 1–8。')
    if not (root / 'data/local/run_configs/default.json').is_file():
        issues.append('尚未初始化；运行 .venv/bin/python scripts/setup_hr.py init。')
    if not supported_platform:
        next_action = "当前试点请改用 macOS；Windows 与其他系统暂不宣称支持。"
    elif not git_available:
        next_action = "先安装 Git，再重新克隆分发仓库。"
    elif python_version < (3, 11):
        next_action = "先安装 Python 3.11 或更高版本，再用它创建本仓库 .venv。"
    elif not in_project_venv:
        next_action = "用刚确认版本不低于 3.11 的 Python 创建 .venv，然后使用 .venv/bin/python。"
    elif not chrome_available:
        next_action = "先安装 Google Chrome，并由本人在官方页面登录 BOSS 招聘方账号。"
    elif any("缺少 boss-agent-cli" in issue or "缺少 patchright" in issue or "版本不匹配" in issue for issue in issues):
        next_action = ".venv/bin/python -m pip install -r requirements.txt -r requirements-boss.txt"
    elif not (root / 'data/local/run_configs/default.json').is_file():
        next_action = ".venv/bin/python scripts/setup_hr.py init"
    elif any("OPENAI_" in issue or "BOSS_HIRE_LLM_WORKERS" in issue for issue in issues):
        next_action = "请本人编辑 .env，填写模型服务根地址和密钥，再重新运行 check。"
    else:
        next_action = "阅读 docs/hr-quickstart.md；登录与真实阶段必须本人操作。"
    return {
        'platform': system_name,
        'supported_platform': supported_platform,
        'python_version': f"{python_version[0]}.{python_version[1]}",
        'using_project_venv': in_project_venv,
        'git_available': git_available,
        'chrome_available': chrome_available,
        'local_ready': not issues,
        'issues': issues,
        'boss_requests': 0,
        'llm_requests': 0,
        'login_verified': False,
        'next': next_action,
        'next_action': next_action,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description='HR 本地初始化与自检，不读取浏览器登录态或访问网络')
    parser.add_argument('action', choices=['init', 'check'])
    args = parser.parse_args(argv)
    if args.action == 'init':
        print(json.dumps({'created': initialize(ROOT), 'existing_files': '保留，不覆盖'}, ensure_ascii=False))
    result = diagnose(ROOT)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.action == 'init':
        return 0
    return 0 if result['local_ready'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
