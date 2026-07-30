"""
Authentication — Playwright login + session capture.

Used to establish a signed-in session with chat.deepseek.com and capture
the bearer token and cookies for the pure HTTP client.
"""

from __future__ import annotations

import json
import os
import platform
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, Optional

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PROFILE_DIR = Path(os.getenv("DEEPSEEK_PROFILE_DIR", ROOT / "session" / "profile"))
DEFAULT_SESSION_FILE = ROOT / "session" / "session.json"

CHAT_URL = "https://chat.deepseek.com/"
SIGNIN_URL = "https://chat.deepseek.com/sign_in"

LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled"]
SESSION_MAX_AGE = 6 * 60 * 60  # 6 hours


def _find_browser() -> Optional[str]:
    """Auto-detect an installed Chrome/Chromium browser based on OS."""
    env_path = os.environ.get("DEEPSEEK_BROWSER_PATH")
    if env_path:
        p = Path(env_path)
        if p.exists():
            return str(p)
        print(f"[auth] DEEPSEEK_BROWSER_PATH='{env_path}' not found; auto-detecting...")

    system = platform.system()

    if system == "Linux":
        candidates = [
            "/usr/bin/google-chrome-stable",
            "/usr/bin/google-chrome",
            "/opt/google/chrome-stable/google-chrome-stable",
            "/opt/google/chrome/google-chrome",
            "/opt/google/chrome/chrome",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/snap/bin/chromium",
            "/var/lib/flatpak/exports/bin/com.google.Chrome",
            "/var/lib/flatpak/exports/bin/org.chromium.Chromium",
        ]
    elif system == "Darwin":
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            os.path.expanduser("~/Applications/Chromium.app/Contents/MacOS/Chromium"),
        ]
    elif system == "Windows":
        candidates = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
            r"C:\Program Files\Chromium\Application\chrome.exe",
            r"C:\Program Files (x86)\Chromium\Application\chrome.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Chromium\Application\chrome.exe"),
        ]
    else:
        candidates = []

    for candidate in candidates:
        if Path(candidate).exists():
            print(f"[auth] Detected browser: {candidate}")
            return candidate

    print("[auth] No system Chrome/Chromium found; falling back to Playwright's bundled browser.")
    return None


class LoginRequired(RuntimeError):
    """Raised when no usable session exists and interactive login is disallowed."""

    DEFAULT = (
        "No valid session found. Log in by running:\n"
        "    python main.py\n"
    )

    def __init__(self, message: str = DEFAULT):
        super().__init__(message)


@dataclass
class Session:
    """A captured signed-in session."""

    token: str
    cookies: Dict[str, str]
    user_agent: str
    captured_at: float

    @property
    def age(self) -> float:
        return time.time() - self.captured_at

    def save(self, path: Path = DEFAULT_SESSION_FILE) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path = DEFAULT_SESSION_FILE) -> Optional["Session"]:
        if not path.exists():
            return None
        try:
            return cls(**json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            return None


_READ_TOKEN_JS = """
() => {
  try {
    const raw = window.localStorage.getItem('userToken');
    if (!raw) return null;
    const o = JSON.parse(raw);
    return (o && o.value) ? o.value : null;
  } catch (e) { return null; }
}
"""


def _safe_evaluate(page, js: str):
    try:
        return page.evaluate(js)
    except Exception as e:
        msg = str(e)
        if "Execution context was destroyed" in msg or "navigation" in msg.lower():
            return None
        raise


def _capture_from_context(context, page) -> Optional[Session]:
    token = _safe_evaluate(page, _READ_TOKEN_JS)
    if not token:
        return None
    cookies = {c["name"]: c["value"] for c in context.cookies()}
    ua = _safe_evaluate(page, "() => navigator.userAgent") or ""
    return Session(token=token, cookies=cookies, user_agent=ua, captured_at=time.time())


def _wait_for_token(page, timeout: float) -> Optional[str]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        token = _safe_evaluate(page, _READ_TOKEN_JS)
        if token:
            return token
        page.wait_for_timeout(1000)
    return None


def _safe_goto(page, url: str) -> None:
    try:
        page.goto(url, wait_until="commit", timeout=60000)
    except Exception as e:
        print(f"[auth] Navigation to {url} was interrupted ({type(e).__name__}); continuing...")
    page.wait_for_timeout(2000)


def login(
    profile_dir: Path = DEFAULT_PROFILE_DIR,
    headless: bool = False,
    assume_logged_out: bool = False,
) -> Session:
    """Interactive login. Opens a visible browser window for manual sign-in."""
    profile_dir.mkdir(parents=True, exist_ok=True)
    browser_path = _find_browser()
    try:
        with sync_playwright() as p:
            kwargs = dict(
                executable_path=browser_path,
                args=LAUNCH_ARGS,
            ) if browser_path else dict(args=LAUNCH_ARGS)
            context = p.chromium.launch_persistent_context(
                str(profile_dir), headless=headless, **kwargs,
            )
            page = context.pages[0] if context.pages else context.new_page()

            existing = None
            if not assume_logged_out:
                _safe_goto(page, CHAT_URL)
                existing = page.evaluate(_READ_TOKEN_JS)

            if not existing:
                _safe_goto(page, SIGNIN_URL)
                print("[auth] Please sign in in the browser window (solve human check if requested).")
                print("[auth] Waiting for active session...")
                if not _wait_for_token(page, timeout=300):
                    context.close()
                    raise RuntimeError("Login timed out — no token captured.")

            session = _capture_from_context(context, page)
            context.close()
            if session is None:
                raise RuntimeError("Logged in but could not read authentication token.")
            session.save()
            return session
    except KeyboardInterrupt:
        print("\n[auth] Login interrupted by user. Goodbye.")
        import sys
        sys.exit(0)


def _headless_refresh(profile_dir: Path) -> Optional[Session]:
    """Try to capture a token headlessly from the persistent profile."""
    profile_dir.mkdir(parents=True, exist_ok=True)
    browser_path = _find_browser()
    session = None
    try:
        with sync_playwright() as p:
            kwargs = dict(
                executable_path=browser_path,
                args=LAUNCH_ARGS,
            ) if browser_path else dict(args=LAUNCH_ARGS)
            context = p.chromium.launch_persistent_context(
                str(profile_dir), headless=True, **kwargs,
            )
            page = context.pages[0] if context.pages else context.new_page()
            try:
                _safe_goto(page, CHAT_URL)
                session = _capture_from_context(context, page)
            finally:
                context.close()
    except KeyboardInterrupt:
        return None

    if session is not None:
        session.save()
    return session


def get_session(
    profile_dir: Path = DEFAULT_PROFILE_DIR,
    session_file: Path = DEFAULT_SESSION_FILE,
    max_age: int = SESSION_MAX_AGE,
    allow_interactive: bool = True,
) -> Session:
    """Return a valid Session. If missing/invalid, launch browser login if allow_interactive=True."""
    cached = Session.load(session_file)
    if cached and cached.age < max_age:
        return cached

    try:
        session = _headless_refresh(profile_dir)
    except KeyboardInterrupt:
        session = None
    if session is not None:
        return session

    if not allow_interactive:
        raise LoginRequired()

    print("[auth] No valid session found — launching browser window to sign in...")
    return login(profile_dir=profile_dir, assume_logged_out=True)


if __name__ == "__main__":
    s = login()
    print(f"[auth] Captured token {s.token[:10]}... ({len(s.cookies)} cookies)")