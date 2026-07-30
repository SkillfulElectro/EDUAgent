"""
Interactive CLI setup & prompt loop for EDUAgent.
"""

import argparse
import json
import os
import sys
from pathlib import Path
from eduagent.agent import EDUAgent


def _state_summary(state: dict) -> str:
    """Build a human-readable summary of a saved state."""
    ws = state.get("work_dir", "?")
    model = state.get("model", "default")
    thinking = "ON" if state.get("thinking") else "OFF"
    search = "ON" if state.get("search") else "OFF"
    policy = state.get("shell_policy", "auto_safe_manual_unsafe")
    hd = state.get("human_delay", True)
    if hd:
        mn = state.get("min_delay", 5.0)
        mx = state.get("max_delay", 10.0)
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


def _delete_state(work_dir: str | Path) -> None:
    """Remove the workspace-specific saved state file(s) if they exist."""
    for fname in ("agent_session.json", "agent_state.json"):
        path = Path(work_dir) / ".eduagent" / fname
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass


def prompt_workspace() -> str:
    """Ask user for the workspace directory only."""
    print("⚙️  EDUAgent Launch Setup")
    print("--------------------------------------------------")
    try:
        ws_input = input("📁 Workspace directory [default: ./workspace]: ").strip()
    except KeyboardInterrupt:
        print("\nAborted.")
        sys.exit(0)
    return str(Path(ws_input if ws_input else "./workspace").expanduser().resolve())


def prompt_remaining_config() -> tuple[str, bool, bool, str, bool, float, float]:
    """Ask user for model, thinking, search, shell policy, and human delay settings
    (workspace has already been determined and resume check performed)."""
    # Model
    print("\n🤖 Select DeepSeek Model:")
    print("  [1] Instant (Fast default model)")
    print("  [2] Expert  (Stronger, slower expert model)")
    try:
        model_choice = input("Enter choice (1/2) [default: 1]: ").strip().lower()
    except KeyboardInterrupt:
        print("\nAborted.")
        sys.exit(0)
    selected_model = "expert" if model_choice in ("2", "expert", "deepseek-expert") else "default"

    # Thinking Mode
    try:
        think_input = input("\n🧠 Enable DeepThink reasoning mode? (y/n) [default: n]: ").strip().lower()
    except KeyboardInterrupt:
        print("\nAborted.")
        sys.exit(0)
    thinking = think_input in ("y", "yes")

    # Search Mode
    try:
        search_input = input("🌐 Enable web search mode? (y/n) [default: n]: ").strip().lower()
    except KeyboardInterrupt:
        print("\nAborted.")
        sys.exit(0)
    search = search_input in ("y", "yes")

    # Shell Execution Safety Policy
    print("\n🛡️  Select Shell Command Execution Policy:")
    print("  [1] Manual Accept             (Prompt before running any command)")
    print("  [2] Auto Accept               (Run all commands automatically)")
    print("  [3] Auto Safe, Reject Unsafe  (Auto-run safe commands, auto-reject unsafe)")
    print("  [4] Auto Safe, Manual Unsafe  (Auto-run safe, prompt for unsafe) [default]")
    try:
        policy_choice = input("Enter choice (1-4) [default: 4]: ").strip()
    except KeyboardInterrupt:
        print("\nAborted.")
        sys.exit(0)

    policies = {
        "1": "manual",
        "2": "auto",
        "3": "auto_safe_reject_unsafe",
        "4": "auto_safe_manual_unsafe",
    }
    shell_policy = policies.get(policy_choice, "auto_safe_manual_unsafe")

    # Human Delay Customization
    print("\n⏱️  Human-like Request Pacing Delay:")
    try:
        delay_enable = input("  Enable randomized delay before requests? (y/n) [default: y]: ").strip().lower()
    except KeyboardInterrupt:
        print("\nAborted.")
        sys.exit(0)
    human_delay = delay_enable not in ("n", "no", "false", "0")
    min_delay, max_delay = 5.0, 10.0
    if human_delay:
        try:
            delay_range = input("  Enter delay range in seconds (min-max) [default: 5.0-10.0]: ").strip()
        except KeyboardInterrupt:
            print("\nAborted.")
            sys.exit(0)
        if delay_range and "-" in delay_range:
            try:
                parts = delay_range.split("-")
                min_delay = float(parts[0].strip())
                max_delay = float(parts[1].strip())
            except ValueError:
                min_delay, max_delay = 5.0, 10.0

    return selected_model, thinking, search, shell_policy, human_delay, min_delay, max_delay


def main():
    parser = argparse.ArgumentParser(description="EDUAgent Interactive Terminal")
    parser.add_argument("--work-dir", "-w", help="Restricted directory for file & shell tools (relative paths and ~ accepted)")
    parser.add_argument("--model", "-m", choices=["default", "expert"], help="Model type")
    parser.add_argument("--thinking", "-t", action="store_true", help="Enable DeepThink reasoning")
    parser.add_argument("--search", "-s", action="store_true", help="Enable web search")
    parser.add_argument("--shell-policy", choices=["manual", "auto", "auto_safe_reject_unsafe", "auto_safe_manual_unsafe"])
    parser.add_argument("--no-delay", action="store_true", help="Disable human-like request pacing delay")
    parser.add_argument("--min-delay", type=float, default=5.0, help="Minimum request delay (seconds)")
    parser.add_argument("--max-delay", type=float, default=10.0, help="Maximum request delay (seconds)")
    parser.add_argument("--no-resume", action="store_true", help="Skip resume prompt and start fresh")
    parser.add_argument("--max-iterations", "-M", type=int, default=20, help="Max tool-call iterations per chat turn (default: 10)")
    args = parser.parse_args()

    # ------------------------------------------------------------------
    #  Step 1: Determine workspace FIRST (CLI arg or interactive prompt)
    # ------------------------------------------------------------------
    resumed = False
    saved_state = None
    state_source = None  # path of the file that was loaded

    if args.work_dir:
        work_dir = str(Path(args.work_dir).expanduser().resolve())
    else:
        work_dir = prompt_workspace()

    # ------------------------------------------------------------------
    #  Step 2: Resume check RIGHT AFTER workspace is known
    #  (skip if --no-resume given).  This ensures agent_state.json and
    #  todo_list.json are always loaded from the same workspace directory.
    # ------------------------------------------------------------------
    if not args.no_resume:
        # Search ONLY the selected workspace directory for a saved session
        for fname in ("agent_session.json", "agent_state.json"):
            cand_path = Path(work_dir) / ".eduagent" / fname
            if cand_path.exists():
                try:
                    saved_state = json.loads(cand_path.read_text(encoding="utf-8"))
                    state_source = str(cand_path)
                except Exception:
                    continue
                if saved_state:
                    break

        if saved_state:
            print(f"\n💾 A previous agent session was found ({state_source}):")
            print(_state_summary(saved_state))
            try:
                ans = input("   Resume this session? (y/n) [default: y]: ").strip().lower()
            except KeyboardInterrupt:
                print("\nAborted.")
                sys.exit(0)
            if ans in ("", "y", "yes"):
                resumed = True
                # Use work_dir from saved state (in case it moved)
                work_dir = saved_state.get("work_dir", work_dir)
                # Verify work_dir still exists; create if missing
                if not Path(work_dir).exists():
                    print(f"⚠️  Saved workspace '{work_dir}' no longer exists.")
                    try:
                        ans2 = input("   Create it now? (y/n) [default: y]: ").strip().lower()
                    except KeyboardInterrupt:
                        print("\nAborted.")
                        sys.exit(0)
                    if ans2 in ("", "y", "yes"):
                        Path(work_dir).mkdir(parents=True, exist_ok=True)
                    else:
                        _delete_state(work_dir)
                        resumed = False
                        saved_state = None
            else:
                # Delete the stale file and don't resume
                if state_source:
                    try:
                        Path(state_source).unlink(missing_ok=True)
                    except Exception:
                        pass
                saved_state = None

    # ------------------------------------------------------------------
    #  Step 3: Get remaining settings (from CLI args, saved state, or prompts)
    # ------------------------------------------------------------------
    if resumed and saved_state:
        # Use settings from saved state — skip prompts entirely
        selected_model = saved_state.get("model", "default")
        thinking = saved_state.get("thinking", False)
        search = saved_state.get("search", False)
        shell_policy = saved_state.get("shell_policy", "auto_safe_manual_unsafe")
        human_delay = saved_state.get("human_delay", True)
        min_delay = saved_state.get("min_delay", 1.0)
        max_delay = saved_state.get("max_delay", 3.0)
    elif args.work_dir and args.model and args.shell_policy:
        # Full CLI args provided — use them directly, skip prompts
        selected_model = args.model
        thinking = args.thinking
        search = args.search
        shell_policy = args.shell_policy
        human_delay = not args.no_delay
        min_delay = args.min_delay
        max_delay = args.max_delay
    else:
        # Prompt for remaining settings (workspace already known)
        (
            selected_model, thinking, search, shell_policy,
            human_delay, min_delay, max_delay,
        ) = prompt_remaining_config()

    # ------------------------------------------------------------------
    #  Step 4: Build the agent
    # ------------------------------------------------------------------
    if resumed and saved_state:
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
    print("💡 Commands: Type '/new' to reconfigure & start fresh, '/move_to_new' to auto-resume via todo list, 'exit' to quit.")
    print("💡 Open https://chat.deepseek.com in your browser to inspect tool calls under the hood!\n")

    def read_multiline() -> str:
        """Read a multiline message from the user.

        First line is read with 'You: ' prompt.  Subsequent lines are read
        with a continuation prompt '...  '.  Input ends when the user enters
        an empty line or the delimiter '/end'.
        """
        lines = []
        first = input("You: ")
        if first.strip().lower() in ("exit", "quit", "/exit", "/new", "/move_to_new"):
            return first.strip()
        lines.append(first)
        while True:
            line = input("...  ")
            stripped = line.strip()
            if stripped == "" or stripped.lower() == "/end":
                break
            lines.append(line)
        return "\n".join(lines)

    def _handle_exhaustion(reply):
        """Prompt user to continue when the agent hits the iteration limit.
        Returns the final reply after all continuations (or the original if declined)."""
        auto_continue = False
        auto_continue_count = 0
        while reply.exhausted:
            if auto_continue:
                auto_continue_count += 1
                if auto_continue_count > 5:
                    print("\n🛑 Auto-continue safety limit (5) reached. Dropping back to prompt.\n")
                    break
                print("⏳ Auto-continuing...\n")
                reply = agent.chat(
                    "Continue where you left off. Check your todo list to resume.",
                    verbose=True,
                    max_tool_iterations=args.max_iterations,
                )
                agent.save_state()
                continue

            print(f"\n⚠️  Agent hit the tool-iteration limit ({args.max_iterations} steps).")
            try:
                tasks_output = agent.todo_tools.list_tasks()
                if tasks_output and "No tasks" not in tasks_output:
                    print("📋 Pending tasks:")
                    print(tasks_output)
            except Exception:
                pass
            try:
                choice = input("   Continue? (y/n/auto) [default: y]: ").strip().lower()
            except KeyboardInterrupt:
                print("\n👋 Interrupted. Exiting exhaustion handler.")
                break
            if choice == "n":
                break
            elif choice == "auto":
                auto_continue = True
                auto_continue_count = 0
                print("⏳ Auto-continuing future exhaustions...")
                reply = agent.chat(
                    "Continue where you left off. Check your todo list to resume.",
                    verbose=True,
                    max_tool_iterations=args.max_iterations,
                )
                agent.save_state()
            else:  # default 'y'
                reply = agent.chat(
                    "Continue where you left off. Check your todo list to resume.",
                    verbose=True,
                    max_tool_iterations=args.max_iterations,
                )
                agent.save_state()
        return reply

    try:
        while True:
            try:
                user_input = read_multiline()
            except KeyboardInterrupt:
                print("\n👋 Interrupted. Saving state and exiting...")
                break
            if not user_input:
                continue
            if user_input.lower() in ("exit", "quit", "/exit"):
                break
            if user_input.lower() == "/new":
                print("\n🔄 Starting a new chat session...")
                work_dir = prompt_workspace()
                # Check for saved state in new workspace before prompting remaining config
                saved_state = None
                for fname in ("agent_session.json", "agent_state.json"):
                    cand_path = Path(work_dir) / ".eduagent" / fname
                    if cand_path.exists():
                        try:
                            saved_state = json.loads(cand_path.read_text(encoding="utf-8"))
                        except Exception:
                            continue
                        if saved_state:
                            break
                if saved_state:
                    print(f"\n💾 A previous agent session was found in '{work_dir}':")
                    print(_state_summary(saved_state))
                    try:
                        ans = input("   Resume this session? (y/n) [default: y]: ").strip().lower()
                    except KeyboardInterrupt:
                        print("\nAborted.")
                        sys.exit(0)
                    if ans in ("", "y", "yes"):
                        work_dir = saved_state.get("work_dir", work_dir)
                        selected_model = saved_state.get("model", "default")
                        thinking = saved_state.get("thinking", False)
                        search = saved_state.get("search", False)
                        shell_policy = saved_state.get("shell_policy", "auto_safe_manual_unsafe")
                        human_delay = saved_state.get("human_delay", True)
                        min_delay = saved_state.get("min_delay", 1.0)
                        max_delay = saved_state.get("max_delay", 3.0)
                        agent = EDUAgent(
                            work_dir=work_dir, model=selected_model,
                            thinking=thinking, search=search,
                            shell_policy=shell_policy,
                            human_delay=human_delay, min_delay=min_delay, max_delay=max_delay,
                        )
                        agent.resume_from_state(saved_state)
                    else:
                        for fname in ("agent_session.json", "agent_state.json"):
                            try:
                                (Path(work_dir) / ".eduagent" / fname).unlink(missing_ok=True)
                            except Exception:
                                pass
                        saved_state = None
                if not saved_state:
                    (
                        selected_model, thinking, search, shell_policy,
                        human_delay, min_delay, max_delay,
                    ) = prompt_remaining_config()
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

            if user_input.strip().lower() == "/move_to_new":
                agent.new_chat()
                agent.save_state()
                print("🔄 Started a new chat session (workspace and settings unchanged).")
                print("📋 Resuming previous task via todo list...\n")
                try:
                    reply = agent.chat(
                        "continue with the task, use your to do list tool to check where you left.",
                        verbose=True,
                        max_tool_iterations=args.max_iterations,
                    )
                except KeyboardInterrupt:
                    print("\n👋 Interrupted during agent response. Saving state...")
                    break
                agent.save_state()
                _handle_exhaustion(reply)
                print()
                continue

            try:
                reply = agent.chat(user_input, verbose=True, max_tool_iterations=args.max_iterations)
            except KeyboardInterrupt:
                print("\n👋 Interrupted during agent response. Saving state...")
                break
            agent.save_state()  # persist state after every response for crash resilience
            _handle_exhaustion(reply)
            print()
    finally:
        # Save state on every graceful exit
        agent.save_state()
        agent.close()


if __name__ == "__main__":
    main()
