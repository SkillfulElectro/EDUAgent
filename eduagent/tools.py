"""
Built-in sandboxed file tools and safe shell execution tool for EDUAgent.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

DANGEROUS_PATTERNS = [
    r"\brm\s+-[rf]*\s+[\/\*]",      # rm -rf / or rm -rf *
    r"\brmdir\s+-[rf]*\s+[\/\*]",
    r"\bmkfs\b",                     # formatting disks
    r"\bdd\b",                        # raw disk writing
    r"\bchmod\s+-[R]*\s+777\b",      # dangerous permissions
    r"\bchown\s+-R\b",               # recursive ownership changes
    r"\bsudo\b",                     # privilege escalation
    r"\bsu\s+",
    r"\bdoas\b",
    r"\bshutdown\b",                 # system state control
    r"\breboot\b",
    r"\bhalt\b",
    r"\bpoweroff\b",
    r"\binit\s+[06]\b",
    r"\bsystemctl\s+(stop|disable|poweroff|reboot)\b",
    r"curl\s+.*\|\s*(bash|sh)",      # piped remote execution
    r"wget\s+.*\|\s*(bash|sh)",
    r"\bkill\s+-9\s+-1\b",           # process killing
    r":\(\)\{\s*:\|:&\s*\};:",      # fork bomb
]


def check_command_safety(command: str) -> Tuple[bool, str]:
    """Inspect a shell command for dangerous or high-risk patterns."""
    cmd_lower = command.lower()
    for pattern in DANGEROUS_PATTERNS:
        if re.search(pattern, cmd_lower):
            return False, f"Matches high-risk pattern: '{pattern}'"
    return True, "Command appears safe"


class FileTools:
    """Sandboxed file operations restricted to `work_dir`."""

    def __init__(self, work_dir: str | Path = "."):
        self.work_dir = Path(work_dir).resolve()
        if not self.work_dir.exists():
            self.work_dir.mkdir(parents=True, exist_ok=True)

    def _safe_path(self, rel_path: str) -> Path:
        """Resolve `rel_path` relative to `work_dir` and enforce sandboxing."""
        target = (self.work_dir / rel_path).resolve()
        try:
            target.relative_to(self.work_dir)
        except ValueError:
            raise PermissionError(
                f"Access denied: Path '{rel_path}' resolves outside restricted work_dir '{self.work_dir}'."
            )
        return target

    def read_file(
        self,
        path: str,
        start_line: Optional[str | int] = None,
        end_line: Optional[str | int] = None,
    ) -> str:
        """Read text content from a file in work_dir, optionally specifying 1-based line range."""
        target = self._safe_path(path)
        if not target.exists():
            return f"Error: File '{path}' does not exist."
        if not target.is_file():
            return f"Error: '{path}' is a directory, not a file."

        content = target.read_text(encoding="utf-8")
        lines = content.splitlines()
        total_lines = len(lines)

        s_line = None
        e_line = None
        if start_line is not None and str(start_line).strip():
            try:
                s_line = int(start_line)
            except ValueError:
                pass
        if end_line is not None and str(end_line).strip():
            try:
                e_line = int(end_line)
            except ValueError:
                pass

        if s_line is None and e_line is None:
            numbered = [f"{i+1:4d} | {line}" for i, line in enumerate(lines)]
            return f"--- File: '{path}' ({total_lines} total lines) ---\n" + "\n".join(numbered)

        start_idx = max(0, (s_line - 1) if s_line is not None else 0)
        end_idx = min(total_lines, e_line if e_line is not None else total_lines)

        if start_idx >= total_lines:
            return f"Error: start_line {s_line} exceeds total file lines ({total_lines})."

        selected = lines[start_idx:end_idx]
        numbered = [f"{i+1:4d} | {line}" for i, line in enumerate(selected, start=start_idx + 1)]
        return f"--- File: '{path}' (Lines {start_idx + 1}-{end_idx} of {total_lines}) ---\n" + "\n".join(numbered)

    def write_file(self, path: str, content: str) -> str:
        """Write or overwrite text content to a file inside work_dir."""
        target = self._safe_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"Successfully wrote to '{path}'."

    def edit_file(self, path: str, old_str: str, new_str: str) -> str:
        """Replace `old_str` with `new_str` inside a file in work_dir."""
        target = self._safe_path(path)
        if not target.exists() or not target.is_file():
            return f"Error: File '{path}' does not exist or is not a file."
        content = target.read_text(encoding="utf-8")

        if old_str in content:
            target.write_text(content.replace(old_str, new_str, 1), encoding="utf-8")
            return f"Successfully edited '{path}'."

        norm_content = content.replace("\r\n", "\n")
        norm_old = old_str.replace("\r\n", "\n")
        norm_new = new_str.replace("\r\n", "\n")
        if norm_old in norm_content:
            target.write_text(norm_content.replace(norm_old, norm_new, 1), encoding="utf-8")
            return f"Successfully edited '{path}' (normalized line endings)."

        lines_content = [line.rstrip() for line in norm_content.split("\n")]
        lines_old = [line.rstrip() for line in norm_old.split("\n")]
        joined_content = "\n".join(lines_content)
        joined_old = "\n".join(lines_old)
        if joined_old in joined_content:
            target.write_text(joined_content.replace(joined_old, norm_new, 1), encoding="utf-8")
            return f"Successfully edited '{path}' (normalized whitespace)."

        if "---" in norm_old:
            alt_old = norm_old.replace("---", "--")
            if alt_old in norm_content:
                target.write_text(norm_content.replace(alt_old, norm_new, 1), encoding="utf-8")
                return f"Successfully edited '{path}' (fuzzy flag match)."

        return f"Error: 'old_str' not found in file '{path}'."

    def list_dir(self, path: str = ".") -> str:
        """List files and subdirectories inside work_dir."""
        target = self._safe_path(path)
        if not target.exists() or not target.is_dir():
            return f"Error: Directory '{path}' does not exist."
        items = []
        for item in sorted(target.iterdir()):
            kind = "DIR" if item.is_dir() else "FILE"
            rel = item.relative_to(self.work_dir)
            items.append(f"[{kind}] {rel}")
        return "\n".join(items) if items else "Directory is empty."

    def ls(self, path: str = ".") -> str:
        """Alias for list_dir."""
        return self.list_dir(path)

    def get_tool_map(self) -> Dict[str, Callable[..., str]]:
        return {
            "read_file": self.read_file,
            "write_file": self.write_file,
            "edit_file": self.edit_file,
            "list_dir": self.list_dir,
            "ls": self.ls,
        }

    def get_tool_definitions(self) -> List[dict]:
        return [
            {
                "name": "read_file",
                "description": "Read text content from a file inside the restricted work directory. Supports line range.",
                "parameters": {
                    "path": "str (relative path to file)",
                    "start_line": "int (optional 1-based start line number)",
                    "end_line": "int (optional 1-based end line number)",
                },
            },
            {
                "name": "write_file",
                "description": "Write or overwrite text content to a file inside the restricted work directory.",
                "parameters": {"path": "str (relative path)", "content": "str (text content to write)"},
            },
            {
                "name": "edit_file",
                "description": "Replace `old_str` with `new_str` in a file inside the restricted work directory.",
                "parameters": {
                    "path": "str (relative path)",
                    "old_str": "str (exact substring to match)",
                    "new_str": "str (replacement substring)",
                },
            },
            {
                "name": "list_dir",
                "description": "List files and subdirectories inside the restricted work directory.",
                "parameters": {"path": "str (relative path, defaults to '.')" },
            },
            {
                "name": "ls",
                "description": "List files and subdirectories inside the restricted work directory (alias for list_dir).",
                "parameters": {"path": "str (relative path, defaults to '.')" },
            },
        ]


class ShellTool:
    """Safe shell command execution tool governed by configurable security policies."""

    def __init__(self, work_dir: Path, policy: str = "auto_safe_manual_unsafe"):
        self.work_dir = Path(work_dir).resolve()
        # Policy options: 'manual', 'auto', 'auto_safe_reject_unsafe', 'auto_safe_manual_unsafe'
        self.policy = policy

    def run_shell_command(self, command: str, timeout: float = 30.0) -> str:
        """Execute a shell command from within work_dir based on safety policy."""
        is_safe, reason = check_command_safety(command)

        approve = False
        if self.policy == "auto":
            approve = True
        elif self.policy == "manual":
            approve = self._prompt_user(command, is_safe, reason)
        elif self.policy == "auto_safe_reject_unsafe":
            if is_safe:
                approve = True
            else:
                return f"Execution Rejected by Policy: Command classified as UNSAFE ({reason})."
        elif self.policy == "auto_safe_manual_unsafe":
            if is_safe:
                approve = True
            else:
                approve = self._prompt_user(command, is_safe, reason)
        else:
            approve = self._prompt_user(command, is_safe, reason)

        if not approve:
            return "Execution Cancelled: User declined command execution."

        try:
            res = subprocess.run(
                command,
                shell=True,
                cwd=str(self.work_dir),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            out = res.stdout.strip()
            err = res.stderr.strip()
            result_lines = [f"[Exit Code: {res.returncode}]"]
            if out:
                result_lines.append(f"STDOUT:\n{out}")
            if err:
                result_lines.append(f"STDERR:\n{err}")
            if not out and not err:
                result_lines.append("(No output returned)")
            return "\n".join(result_lines)
        except subprocess.TimeoutExpired:
            return f"Execution Error: Command timed out after {timeout} seconds."
        except Exception as e:
            return f"Execution Error: {e}"

    def _prompt_user(self, command: str, is_safe: bool, reason: str) -> bool:
        status_str = "SAFE" if is_safe else f"UNSAFE ({reason})"
        print(f"\n⚠️  [Shell Command Approval Required]")
        print(f"   Command: $ {command}")
        print(f"   Safety Rating: {status_str}")
        choice = input("   Execute this command? (y/n) [default: n]: ").strip().lower()
        return choice in ("y", "yes")

    def get_tool_map(self) -> Dict[str, Callable[..., str]]:
        return {"run_shell_command": self.run_shell_command}

    def get_tool_definitions(self) -> List[dict]:
        return [
            {
                "name": "run_shell_command",
                "description": "Execute a shell command inside the workspace directory.",
                "parameters": {"command": "str (shell command to run)"},
            }
        ]