from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Any, Callable

from boss_hire.local_security import ensure_private_directory, ensure_private_file


CookieExtractor = Callable[..., dict[str, Any] | None]


def session_fingerprint(auth_payload: dict[str, Any]) -> str:
    cookies = auth_payload.get("cookies") if isinstance(auth_payload, dict) else None
    primary_cookie = str(cookies.get("wt2") or "") if isinstance(cookies, dict) else ""
    if not primary_cookie:
        raise RuntimeError("BOSS 登录态缺少 wt2，不能生成会话指纹")
    return hashlib.sha256(f"boss-session-v1\0{primary_cookie}".encode("utf-8")).hexdigest()[:24]


def load_saved_session_fingerprint(auth_dir: Path) -> str:
    from boss_agent_cli.auth.token_store import TokenStore

    saved = TokenStore(Path(auth_dir) / "auth").load()
    if not isinstance(saved, dict):
        raise RuntimeError("固定认证目录中没有可用的 BOSS 登录态")
    return session_fingerprint(saved)


def sync_auth_from_chrome(
    auth_dir: Path,
    *,
    extractor: CookieExtractor | None = None,
    input_fn: Callable[[str], str] = input,
) -> dict[str, Any]:
    """Persist the current Chrome BOSS session before an explicit live run."""
    if extractor is None:
        from boss_agent_cli.auth.cookie_extract import extract_cookies

        extractor = extract_cookies
    try:
        current = extractor("chrome", platform="zhipin")
    except Exception as exc:
        raise RuntimeError(
            "Chrome 登录态提取失败；未覆盖已保存认证。请本人核对 Chrome 登录、系统钥匙串权限和 Profile，"
            "将系统类型与错误类别交给助手诊断，不要复制 Cookie。错误类别：" + type(exc).__name__
        ) from None
    cookies = current.get("cookies") if isinstance(current, dict) else None
    if not isinstance(cookies, dict) or not str(cookies.get("wt2") or ""):
        raise RuntimeError("Chrome 中未找到当前 BOSS wt2 登录态；请本人在 Chrome 官方页面登录并核对 Profile，未覆盖已保存认证")

    from boss_agent_cli.auth.token_store import TokenStore

    target_root = Path(auth_dir)
    token_root = target_root / "auth"
    ensure_private_directory(target_root)
    ensure_private_directory(token_root)
    store = TokenStore(token_root)
    try:
        previous = store.load()
    except Exception as exc:
        raise RuntimeError("本地认证读取失败；请检查本机权限，保留认证与账本。错误类别：" + type(exc).__name__) from None
    previous_cookies = previous.get("cookies") if isinstance(previous, dict) else None
    previous_primary = previous_cookies.get("wt2") if isinstance(previous_cookies, dict) else None
    session_changed = bool(previous_primary and str(previous_primary) != str(cookies["wt2"]))
    if session_changed:
        try:
            answer = input_fn(
                "登录会话变化，不能自动判断是否换账号。请在官方页面核对仍为本安装原账号；"
                "若换了账号请停止。输入“确认仍为本人原账号”继续："
            )
        except EOFError:
            answer = ""
        if answer.strip() != "确认仍为本人原账号":
            raise RuntimeError("会话变化未获原账号确认；未覆盖认证、未签发授权，请人工核对")
    changed = previous != current
    backup_created = False
    session_path = token_root / "session.enc"
    backup_path = token_root / "session.enc.pre-chrome-sync"
    if changed and session_path.is_file() and not backup_path.exists():
        shutil.copy2(session_path, backup_path)
        ensure_private_file(backup_path)
        backup_created = True
    if changed:
        store.save(current)
        ensure_private_file(session_path)

    return {
        "source": "chrome",
        "changed": changed,
        "session_changed": session_changed,
        "identity_verified": False,
        "backup_created": backup_created,
        "cookie_count": len(cookies),
        "stoken_present": bool(current.get("stoken")),
        "user_agent_present": bool(current.get("user_agent")),
        "session_fingerprint": session_fingerprint(current),
    }
