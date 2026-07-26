"""
Interactive CLI setup & prompt loop for EDUAgent.
"""

import argparse
import os
import sys
from pathlib import Path
from eduagent.agent import EDUAgent, STATE_FILE


def _state_summary(state: dict) -> str:
    """Build a human-readable summary of a saved state."""
    ws = state.get("work_dir", "?")
    model = state.get("model", "default")
    thinking = "ON" if state.get("thinking") else "OFF"
    search = "ON" if state.get("search") else "OFF"
    policy = state.get("shell_policy", "auto_safe_manual_unsafe")
    hd = state.get("human_delay", True)
    if hd:
        mn = state.get("min_delay", 1.0)
        mx = state.get("max_delay", 3.0)
        delay_str = f"{mn}s-{mx}s"
    else:
        delay_str = "Disabled"
    saved_ts = state.get("saved_at", 0)
    import time as _time
    saved_str = _time.strftime("%Y-%m-%d %H:%M:%S", _time.localtime(saved_ts)) if saved_ts else "unknown"

    return (
        f"   Workspace : {ws}\n"
        f"   Model     : {model}\n"
        f"   Thinking  : {thinking}\n"
        f"   Search    : {search}\n"
        f"   Shell Pol.: {policy}\n"
        f"   Delay     : {delay_str}\n"
        f"   Saved at  : {saved_str}"
    )


def _delete_state() -> None:
    """Remove the saved state file if it exists."""
    try:
        STATE_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def _has_explicit_cli_args(args: argparse.Namespace) -> bool:
    """Return True if any config-relevant CLI arg was explicitly provided."""
    return bool(
        args.work_dir
        or args.model
        or args.thinking
        or args.search
        or args.shell_policy
        or args.no_delay
        or args.min_delay != 1.0
        or args.max_delay != 3.0
    )


def prompt_agent_configuration() -> tuple[str, str, bool, bool, str, bool, float, float]:
    """Ask user for workspace, model, thinking, search, shell policy, and human delay settings."""
    print("⚙️  EDUAgent Launch Setup")
    print("--------------------------------------------------")

    # 1. Workspace
    ws_input = input("📁 Workspace directory [default: ./workspace]: ").strip()
    work_dir = str(Path(ws_input if ws_input else "./workspace").expanduser().resolve())

    # 2. Model
    print("\n🤖 Select DeepSeek Model:")
    print("  [1] Instant (Fast default model)")
    print("  [2] Expert  (Stronger, slower expert model)")
    model_choice = input("Enter choice (1/2) [default: 1]: ").strip().lower()
    selected_model = "expert" if model_choice in ("2", "expert", "deepseek-expert") else "default"

    # 3. Thinking Mode
    think_input = input("\n🧠 Enable DeepThink reasoning mode? (y/n) [default: n]: ").strip().lower()
    thinking = think_input in ("y", "yes")

    # 4. Search Mode
    search_input = input("🌐 Enable web search mode? (y/n) [default: n]: ").strip().lower()
    search = search_input in ("y", "yes")

    # 5. Shell Execution Safety Policy
    print("\n🛡️  Select Shell Command Execution Policy:")
    print("  [1] Manual Accept             (Prompt before running any command)")
    print("  [2] Auto Accept               (Run all commands automatically)")
    print("  [3] Auto Safe, Reject Unsafe  (Auto-run safe commands, auto-reject unsafe)")
    print("  [4] Auto Safe, Manual Unsafe  (Auto-run safe, prompt for unsafe) [default]")
    policy_choice = input("Enter choice (1-4) [default: 4]: ").strip()

    policies = {
        "1": "manual",
        "2": "auto",
        "3": "auto_safe_reject_unsafe",
        "4": "auto_safe_manual_unsafe",
    }
    shell_policy = policies.get(policy_choice, "auto_safe_manual_unsafe")

    # 6. Human Delay Customization
    print("\n⏱️  Human-like Request Pacing Delay:")
    delay_enable = input("  Enable randomized delay before requests? (y/n) [default: y]: ").strip().lower()
    human_delay = delay_enable not in ("n", "no", "false", "0")
    min_delay, max_delay = 1.0, 3.0
    if human_delay:
        delay_range = input("  Enter delay range in seconds (min-max) [default: 1.0-3.0]: ").strip()
        if delay_range and "-" in delay_range:
            try:
                parts = delay_range.split("-")
                min_delay = float(parts[0].strip())
                max_delay = float(parts[1].strip())
            except ValueError:
                min_delay, max_delay = 1.0, 3.0

    return work_dir, selected_model, thinking, search, shell_policy, human_delay, min_delay, max_delay


def main():
    parser = argparse.ArgumentParser(description="EDUAgent Interactive Terminal")
    parser.add_argument("--work-dir", "-w", help="Restricted directory for file & shell tools (relative paths and ~ accepted)")
    parser.add_argument("--model", "-m", choices=["default", "expert"], help="Model type")
    parser.add_argument("--thinking", "-t", action="store_true", help="Enable DeepThink reasoning")
    parser.add_argument("--search", "-s", action="store_true", help="Enable web search")
    parser.add_argument("--shell-policy", choices=["manual", "auto", "auto_safe_reject_unsafe", "auto_safe_manual_unsafe"])
    parser.add_argument("--no-delay", action="store_true", help="Disable human-like request pacing delay")
    parser.add_argument("--min-delay", type=float, default=1.0, help="Minimum request delay (seconds)")
    parser.add_argument("--max-delay", type=float, default=3.0, help="Maximum request delay (seconds)")
    parser.add_argument("--no-resume", action="store_true", help="Skip resume prompt and start fresh")
    args = parser.parse_args()

    # ------------------------------------------------------------------
    #  Resume-on-startup: check for saved state (skip if CLI args given)
    # ------------------------------------------------------------------
    resumed = False
    saved_state = None
    if not args.no_resume and not _has_explicit_cli_args(args):
        saved_state = EDUAgent.load_state()
        if saved_state:
            print("💾 A previous session was found:")
            print(_state_summary(saved_state))
            ans = input("   Resume this session? (y/n) [default: y]: ").strip().lower()
            if ans in ("", "y", "yes"):
                resumed = True
            else:
                _delete_state()
                saved_state = None

    if resumed and saved_state:
        work_dir = saved_state.get("work_dir", "./workspace")
        # Verify work_dir still exists; fall back to prompt if missing
        if not Path(work_dir).exists():
            print(f"⚠️  Saved workspace '{work_dir}' no longer exists.")
            ans2 = input("   Create it now? (y/n) [default: y]: ").strip().lower()
            if ans2 in ("", "y", "yes"):
                Path(work_dir).mkdir(parents=True, exist_ok=True)
            else:
                _delete_state()
                resumed = False
                saved_state = None

    if resumed and saved_state:
        # Build agent with saved settings
        work_dir = saved_state.get("work_dir", "./workspace")
        selected_model = saved_state.get("model", "default")
        thinking = saved_state.get("thinking", False)
        search = saved_state.get("search", False)
        shell_policy = saved_state.get("shell_policy", "auto_safe_manual_unsafe")
        human_delay = saved_state.get("human_delay", True)
        min_delay = saved_state.get("min_delay", 1.0)
        max_delay = saved_state.get("max_delay", 3.0)

        agent = EDUAgent(
            work_dir=work_dir,
            model=selected_model,
            thinking=thinking,
            search=search,
            shell_policy=shell_policy,
            human_delay=human_delay,
            min_delay=min_delay,
            max_delay=max_delay,
        )
        agent.resume_from_state(saved_state)
    else:
        # Normal startup: prompt or use CLI args
        if not (args.work_dir and args.model and args.shell_policy):
            work_dir, selected_model, thinking, search, shell_policy, human_delay, min_delay, max_delay = (
                prompt_agent_configuration()
            )
        else:
            work_dir = str(Path(args.work_dir).expanduser().resolve())
            selected_model = args.model
            thinking = args.thinking
            search = args.search
            shell_policy = args.shell_policy
            human_delay = not args.no_delay
            min_delay = args.min_delay
            max_delay = args.max_delay

        agent = EDUAgent(
            work_dir=work_dir,
            model=selected_model,
            thinking=thinking,
            search=search,
            shell_policy=shell_policy,
            human_delay=human_delay,
            min_delay=min_delay,
            max_delay=max_delay,
        )

    print(f"\n🤖 EDUAgent Active!")
    print(f"📁 Workspace Directory : {agent.file_tools.work_dir}")
    print(f"🧠 DeepThink Reasoning : {'ON' if agent.thinking else 'OFF'}")
    print(f"🌐 Web Search Mode    : {'ON' if agent.search else 'OFF'}")
    print(f"🛡️ Shell Policy       : {agent.shell_tool.policy}")
    if agent.client.human_delay:
        print(f"⏱️ Request Pacing      : {agent.client.min_delay}s - {agent.client.max_delay}s")
    else:
        print("⏱️ Request Pacing      : Disabled")
    print("💡 Commands: Type '/new' to start a new chat, 'exit' to quit.")
    print("💡 Open https://chat.deepseek.com in your browser to inspect tool calls under the hood!\n")

    def read_multiline() -> str:
        """Read a multiline message from the user.

        First line is read with 'You: ' prompt.  Subsequent lines are read
        with a continuation prompt '...  '.  Input ends when the user enters
        an empty line or the delimiter '/end'.
        """
        lines = []
        first = input("You: ")
        if first.strip().lower() in ("exit", "quit", "/exit", "/new"):
            return first.strip()
        lines.append(first)
        while True:
            line = input("...  ")
            stripped = line.strip()
            if stripped == "" or stripped.lower() == "/end":
                break
            lines.append(line)
        return "\
".join(lines)

    try:
        while True:
            user_input = read_multiline()
            if not user_input:
                continue
            if user_input.lower() in ("exit", "quit", "/exit"):
                break
            if user_input.lower() == "/new":
                print("\n🔄 Starting a new chat session...")
                work_dir, selected_model, thinking, search, shell_policy, human_delay, min_delay, max_delay = (
                    prompt_agent_configuration()
                )
                agent.new_chat(model=selected_model)
                agent.thinking = thinking
                agent.search = search
                agent.shell_tool.policy = shell_policy
                agent.client.human_delay = human_delay
                agent.client.min_delay = min_delay
                agent.client.max_delay = max_delay
                # Persist the new config immediately
                agent.save_state()
                print(f"🔄 Switched to new chat session.\n")
                continue

            agent.chat(user_input, verbose=True)
            print()
    finally:
        # Save state on every graceful exit
        agent.save_state()
        agent.close()


if __name__ == "__main__":
    main()