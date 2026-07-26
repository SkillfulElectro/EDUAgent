"""
EDUAgent — Single Entrypoint.

Checks if the user is logged into DeepSeek.
If not logged in, launches a browser window for interactive login.
Once authenticated, launches the interactive EDUAgent interface.
"""

from __future__ import annotations

import sys
from dotenv import load_dotenv

from eduagent.auth import get_session
from eduagent.cli import main as cli_main


def main():
    load_dotenv()
    print("==================================================")
    print("               Welcome to EDUAgent                ")
    print("==================================================")
    print("[EDUAgent] Checking DeepSeek authentication status...")

    try:
        # get_session automatically checks stored session / persistent profile.
        # If no valid session is found, allow_interactive=True launches browser window for login.
        session = get_session(allow_interactive=True)
        print(f"[EDUAgent] Authenticated successfully! (Token: {session.token[:10]}...)")

        # Quick connectivity check to DeepSeek API
        import httpx
        print("[EDUAgent] Checking connectivity to DeepSeek API...")
        try:
            test_client = httpx.Client(timeout=httpx.Timeout(connect=10.0))
            test_client.get("https://chat.deepseek.com/api/v0/chat_session/create",
                           headers={"authorization": f"Bearer {session.token}"})
            print("[EDUAgent] Connectivity OK.")
        except Exception as e:
            print(f"[EDUAgent] WARNING: Connectivity check failed: {e}")
            print("[EDUAgent] The agent will still start, but API calls may fail.")
    except Exception as e:
        print(f"[EDUAgent] Authentication failed: {e}")
        sys.exit(1)

    print("[EDUAgent] Starting interactive agent loop...\n")
    cli_main()


if __name__ == "__main__":
    main()