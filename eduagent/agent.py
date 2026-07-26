"""
EDUAgent — stateful agent managing single-chat threads on DeepSeek with sandboxed file tools,
safe shell interaction, MCP servers, and live response streaming with tool call suppression.
"""

from __future__ import annotations

import ast
import html
import json
import re
import sys
from pathlib import Path
from typing import Callable, Iterator, List, Optional, Tuple

from .client import DeepSeekClient, Reply
from .mcp import MCPManager
from .tools import FileTools, ShellTool


class ToolCallStreamFilter:
    """Filters out raw <tool_call> tags and JSON blocks from live terminal output,
    replacing them with 'tool call . . . . .'.
    """

    def __init__(self, on_text: Callable[[str], None], on_tool_call_start: Callable[[], None]):
        self.on_text = on_text
        self.on_tool_call_start = on_tool_call_start
        self.buffer = ""
        self.in_tool_call = False
        self.indicator_printed = False

    def feed(self, chunk: str) -> None:
        self.buffer += chunk

        while self.buffer:
            if not self.in_tool_call:
                # Regex matching any variant of tool call trigger
                match = re.search(
                    r"<\?tool_call>|<\?tool_call|<tool_call>|<tool_call|tool_call>|\btool_call\b|\{\s*\"(?:name|arguments)\"\s*:",
                    self.buffer,
                )
                if match:
                    start_pos = match.start()
                    if start_pos > 0:
                        prefix = self.buffer[:start_pos]
                        self.on_text(prefix)

                    self.buffer = self.buffer[match.end():]
                    self.in_tool_call = True

                    if not self.indicator_printed:
                        self.on_tool_call_start()
                        self.indicator_printed = True
                else:
                    # Check for partial triggers at end of buffer
                    tail_match = re.search(r"<[?a-zA-Z_0-9]*$|t[ool_ca]*$|\{\s*\"?[a-zA-Z]*$", self.buffer)
                    if tail_match:
                        safe_len = tail_match.start()
                        if safe_len > 0:
                            safe_text = self.buffer[:safe_len]
                            self.on_text(safe_text)
                            self.buffer = self.buffer[safe_len:]
                        break
                    else:
                        self.on_text(self.buffer)
                        self.buffer = ""
            else:
                end_match = re.search(r"</tool_call>|</tool_call", self.buffer)
                if end_match:
                    end_pos = end_match.end()
                    self.buffer = self.buffer[end_pos:]
                    self.in_tool_call = False
                else:
                    # Keep suppressing tool call payload
                    break

    def flush(self) -> None:
        if not self.in_tool_call and self.buffer:
            self.on_text(self.buffer)
            self.buffer = ""
        self.buffer = ""
        self.in_tool_call = False
        self.indicator_printed = False


def repair_invalid_escapes(s: str) -> str:
    return re.sub(r'\\(?![\\"/bfnrt]|u[0-9a-fA-F]{4})', r'\\\\', s)


def format_tool_args(args: dict) -> str:
    """Summarize tool argument values to keep terminal logs concise (e.g. write_file content)."""
    formatted = []
    for k, v in args.items():
        if k == "content" and isinstance(v, str):
            lines = len(v.splitlines())
            chars = len(v)
            formatted.append(f"{k}='<{lines} lines, {chars} chars>'")
        elif isinstance(v, str) and len(v) > 120:
            truncated = v[:80].replace("\n", "\\n")
            formatted.append(f"{k}={repr(truncated + f'... [truncated {len(v)} chars]')}")
        else:
            formatted.append(f"{k}={repr(v)}")
    return ", ".join(formatted)


def extract_tool_call_heuristically(text: str) -> Optional[dict]:
    name_match = re.search(r'"name"\s*:\s*"([^"]+)"', text) or re.search(r"'name'\s*:\s*'([^']+)'", text)
    if not name_match:
        return None
    tool_name = name_match.group(1)

    args_match = re.search(r'"arguments"\s*:\s*\{', text) or re.search(r"'arguments'\s*:\s*\{", text)
    if not args_match:
        return {"name": tool_name, "arguments": {}}

    args_str = text[args_match.end():]

    arg_keys = re.findall(r'"(\w+)"\s*:\s*"|\'(\w+)\'\s*:\s*\'', args_str)
    keys = [k[0] or k[1] for k in arg_keys]

    if not keys:
        return {"name": tool_name, "arguments": {}}

    arguments = {}
    key_positions = []
    for k in keys:
        m = re.search(r'["\']' + re.escape(k) + r'["\']\s*:\s*["\']', args_str)
        if m:
            key_positions.append((m.start(), m.end(), k))

    key_positions.sort()

    for i in range(len(key_positions)):
        pos_start, val_start, k = key_positions[i]
        if i + 1 < len(key_positions):
            val_end = key_positions[i+1][0]
            raw_val = args_str[val_start:val_end]
            raw_val = re.sub(r'["\']\s*,\s*$', '', raw_val.rstrip())
        else:
            val_end = args_str.rfind('}')
            if val_end == -1:
                val_end = len(args_str)
            raw_val = args_str[val_start:val_end]
            raw_val = re.sub(r'["\']\s*\}*\s*$', '', raw_val.rstrip())

        val_clean = raw_val.replace('\\"', '"').replace('\\n', '\n').replace('\\t', '\t').replace('\\\\', '\\')
        arguments[k] = val_clean

    return {"name": tool_name, "arguments": arguments}


def parse_json_super_lenient(text: str) -> Optional[dict]:
    text = text.strip()
    if not text:
        return None

    try:
        return json.loads(text)
    except Exception:
        pass

    try:
        return json.loads(text, strict=False)
    except Exception:
        pass

    try:
        html_unescaped = html.unescape(text)
        return json.loads(html_unescaped, strict=False)
    except Exception:
        pass

    try:
        fixed = repair_invalid_escapes(text)
        return json.loads(fixed, strict=False)
    except Exception:
        pass

    try:
        py_text = re.sub(r'\btrue\b', 'True', text)
        py_text = re.sub(r'\bfalse\b', 'False', py_text)
        py_text = re.sub(r'\bnull\b', 'None', py_text)
        parsed = ast.literal_eval(py_text)
        if isinstance(parsed, dict) and "name" in parsed:
            return parsed
    except Exception:
        pass

    try:
        heur = extract_tool_call_heuristically(text)
        if heur and isinstance(heur, dict) and "name" in heur:
            return heur
    except Exception:
        pass

    return None


def find_balanced_json_end(text: str, start_idx: int) -> int:
    brace_count = 0
    in_string = False
    escape = False
    for j in range(start_idx, len(text)):
        c = text[j]
        if escape:
            escape = False
            continue
        if c == "\\" and in_string:
            escape = True
            continue
        if c == '"':
            in_string = not in_string
            continue
        if not in_string:
            if c == "{":
                brace_count += 1
            elif c == "}":
                brace_count -= 1
                if brace_count == 0:
                    return j
    return -1


def _extract_and_clean_tool_calls(
    text: str, thinking: Optional[str]
) -> Tuple[List[dict], str, Optional[str]]:
    clean_text = text or ""
    clean_thinking = thinking or ""
    full_combined = (clean_thinking + "\n" + clean_text).strip()
    tool_calls = []

    xml_matches = re.findall(r"<?tool_call>(.*?)(?:</tool_call>|$)", full_combined, re.DOTALL)
    for match in xml_matches:
        cleaned = match.strip()
        if cleaned.startswith("```json"):
            cleaned = cleaned[7:]
        if cleaned.startswith("```"):
            cleaned = cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()
        if not cleaned:
            continue
        parsed = parse_json_super_lenient(cleaned)
        if parsed and isinstance(parsed, dict) and "name" in parsed:
            tool_calls.append(parsed)

    if not tool_calls:
        md_matches = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", full_combined, re.DOTALL)
        for match in md_matches:
            parsed = parse_json_super_lenient(match)
            if parsed and isinstance(parsed, dict) and "name" in parsed:
                tool_calls.append(parsed)

    if not tool_calls:
        for start_match in re.finditer(r'\{\s*"(?:name|arguments)"\s*:', full_combined):
            start_idx = start_match.start()
            end_idx = find_balanced_json_end(full_combined, start_idx)
            if end_idx != -1:
                candidate_json = full_combined[start_idx : end_idx + 1]
                parsed = parse_json_super_lenient(candidate_json)
                if parsed and isinstance(parsed, dict) and "name" in parsed:
                    tool_calls.append(parsed)
            else:
                candidate = full_combined[start_idx:]
                r_end = candidate.rfind("}")
                if r_end != -1:
                    candidate_json = candidate[: r_end + 1]
                    parsed = parse_json_super_lenient(candidate_json)
                    if parsed and isinstance(parsed, dict) and "name" in parsed:
                        tool_calls.append(parsed)

    if not tool_calls:
        heur = extract_tool_call_heuristically(full_combined)
        if heur and isinstance(heur, dict) and "name" in heur:
            tool_calls.append(heur)

    if tool_calls:
        if clean_thinking:
            clean_thinking = re.sub(
                r"<?tool_call>.*?(?:</tool_call>|$)", "", clean_thinking, flags=re.DOTALL
            )
            for tc in tool_calls:
                tc_name = tc.get("name")
                if tc_name:
                    clean_thinking = re.sub(
                        r"\{\s*\"name\"\s*:\s*\"" + re.escape(tc_name) + r"\".*?\}",
                        "",
                        clean_thinking,
                        flags=re.DOTALL,
                    )
            clean_thinking = clean_thinking.strip()

        if clean_text:
            clean_text = re.sub(
                r"<?tool_call>.*?(?:</tool_call>|$)", "", clean_text, flags=re.DOTALL
            )
            for tc in tool_calls:
                tc_name = tc.get("name")
                if tc_name:
                    clean_text = re.sub(
                        r"\{\s*\"name\"\s*:\s*\"" + re.escape(tc_name) + r"\".*?\}",
                        "",
                        clean_text,
                        flags=re.DOTALL,
                    )
            clean_text = clean_text.strip()

    return tool_calls, clean_text, (clean_thinking if clean_thinking else None)


class EDUAgent:
    """Stateful agent managing single-chat threads with sandboxed tools, shell execution & MCP servers."""

    def __init__(
        self,
        client: Optional[DeepSeekClient] = None,
        work_dir: str | Path = "./workspace",
        model: str = "default",
        thinking: bool = False,
        search: bool = False,
        shell_policy: str = "auto_safe_manual_unsafe",
        human_delay: bool = True,
        min_delay: float = 1.0,
        max_delay: float = 3.0,
        system_prompt: Optional[str] = None,
        mcp_servers: Optional[List[List[str] | str]] = None,
        mcp_config_path: Optional[str | Path] = "mcp_servers.json",
    ):
        self.client = client or DeepSeekClient(
            human_delay=human_delay,
            min_delay=min_delay,
            max_delay=max_delay,
        )
        self.model = model
        self.thinking = thinking
        self.search = search
        self.system_prompt = system_prompt
        self._conversation_id: Optional[str] = None

        self.file_tools = FileTools(work_dir)
        self.shell_tool = ShellTool(self.file_tools.work_dir, policy=shell_policy)
        self.mcp_manager = MCPManager()

        if mcp_config_path:
            self.mcp_manager.load_from_json(mcp_config_path)

        if mcp_servers:
            for server_cmd in mcp_servers:
                self.add_mcp_server(server_cmd)

    def add_mcp_server(self, command: List[str] | str, server_name: Optional[str] = None) -> None:
        self.mcp_manager.add_server(command, server_name=server_name)

    @property
    def conversation_id(self) -> Optional[str]:
        return self._conversation_id

    @conversation_id.setter
    def conversation_id(self, cid: Optional[str]) -> None:
        self._conversation_id = cid

    def new_chat(
        self,
        model: Optional[str] = None,
        system_prompt: Optional[str] = None,
    ) -> None:
        self._conversation_id = None
        if model is not None:
            self.model = model
        if system_prompt is not None:
            self.system_prompt = system_prompt

    def _build_system_prompt(self) -> str:
        all_defs = (
            self.file_tools.get_tool_definitions()
            + self.shell_tool.get_tool_definitions()
            + self.mcp_manager.get_tool_definitions()
        )

        lines = []
        if self.system_prompt:
            lines.append(self.system_prompt)
            lines.append("")

        lines.append("You are EDUAgent, an autonomous AI Agent equipped with file, shell, and tool capabilities.")
        lines.append(f"ALL file operations and shell command execution are RESTRICTED to the workspace directory: '{self.file_tools.work_dir}'.")
        lines.append("\nAvailable Tools:")
        for t in all_defs:
            lines.append(f"- Name: {t['name']}")
            lines.append(f"  Description: {t['description']}")
            lines.append(f"  Parameters: {json.dumps(t['parameters'])}")

        lines.append("""
To execute a tool, format your output strictly as a JSON object inside <tool_call> tags:
<tool_call>
{
  "name": "tool_name",
  "arguments": {
    "param1": "value1"
  }
}
</tool_call>

You can perform one tool call per turn. After receiving <tool_result>, present your analysis or final answer.
""")
        return "\n".join(lines)

    def _prepare_prompt(self, message: str) -> str:
        if self._conversation_id is None:
            sys_instr = self._build_system_prompt()
            return f"System Instructions:\n{sys_instr}\n\nUser: {message}"
        return message

    def _execute_tool(self, name: str, arguments: dict) -> str:
        file_map = self.file_tools.get_tool_map()
        if name in file_map:
            try:
                return file_map[name](**arguments)
            except Exception as e:
                return f"File Tool Error: {e}"

        shell_map = self.shell_tool.get_tool_map()
        if name in shell_map:
            try:
                return shell_map[name](**arguments)
            except Exception as e:
                return f"Shell Tool Error: {e}"

        if self.mcp_manager.has_tool(name):
            try:
                return self.mcp_manager.call_tool(name, arguments)
            except Exception as e:
                return f"MCP Error: {e}"

        return f"Error: Tool '{name}' is not registered."

    def chat(
        self,
        message: str,
        thinking: Optional[bool] = None,
        search: Optional[bool] = None,
        max_tool_iterations: int = 10,
        verbose: bool = True,
    ) -> Reply:
        prompt = self._prepare_prompt(message)
        use_thinking = self.thinking if thinking is None else thinking
        use_search = self.search if search is None else search

        current_prompt = prompt
        iteration = 0
        last_reply = None

        while iteration < max_tool_iterations:
            iteration += 1
            model_type = self.model if self._conversation_id is None else None

            stream_obj = self.client.stream(
                prompt=current_prompt,
                conversation_id=self._conversation_id,
                model=model_type,
                thinking=use_thinking,
                search=use_search,
                stream_events=True,
            )

            text_chunks = []
            thinking_chunks = []
            printed_think_header = False
            printed_text_header = False

            def think_writer(text: str):
                nonlocal printed_think_header
                if not text or not verbose:
                    return
                if not printed_think_header:
                    sys.stdout.write("\n🧠 Thinking:\n")
                    printed_think_header = True
                sys.stdout.write(text)
                sys.stdout.flush()

            def on_tool_call_in_think():
                if verbose:
                    sys.stdout.write("\ntool call . . . . .\n")
                    sys.stdout.flush()

            def text_writer(text: str):
                nonlocal printed_text_header
                if not text or not verbose:
                    return
                if not printed_text_header:
                    if printed_think_header:
                        sys.stdout.write("\n\n")
                    sys.stdout.write("🤖 DeepSeek:\n")
                    printed_text_header = True
                sys.stdout.write(text)
                sys.stdout.flush()

            def on_tool_call_in_text():
                if verbose:
                    sys.stdout.write("\ntool call . . . . .\n")
                    sys.stdout.flush()

            think_filter = ToolCallStreamFilter(on_text=think_writer, on_tool_call_start=on_tool_call_in_think)
            text_filter = ToolCallStreamFilter(on_text=text_writer, on_tool_call_start=on_tool_call_in_text)

            for event in stream_obj:
                etype, chunk = event if isinstance(event, tuple) else ("text", event)
                if etype == "think":
                    thinking_chunks.append(chunk)
                    think_filter.feed(chunk)
                elif etype == "text":
                    text_chunks.append(chunk)
                    text_filter.feed(chunk)

            think_filter.flush()
            text_filter.flush()

            if verbose and (printed_think_header or printed_text_header):
                sys.stdout.write("\n")
                sys.stdout.flush()

            self._conversation_id = stream_obj.conversation_id
            full_text = "".join(text_chunks)
            full_thinking = "".join(thinking_chunks) or stream_obj.thinking

            tool_calls, clean_text, clean_thinking = _extract_and_clean_tool_calls(
                full_text, full_thinking
            )

            reply = Reply(
                text=clean_text,
                conversation_id=self._conversation_id,
                thinking=clean_thinking,
            )
            last_reply = reply

            if not tool_calls:
                return reply

            results = []
            for call in tool_calls:
                t_name = call.get("name")
                t_args = call.get("arguments", {})

                if verbose:
                    args_summary = format_tool_args(t_args)
                    print(f"\n🛠️ Executing tool: {t_name}({args_summary})")

                res = self._execute_tool(t_name, t_args)

                if verbose:
                    lines_res = res.split("\n") if res else ["No output."]
                    output_summary = "\n".join("     " + line for line in lines_res[:10])
                    if len(lines_res) > 10:
                        output_summary += f"\n     ... ({len(lines_res) - 10} more lines)"
                    print(f"   Output:\n{output_summary}\n")

                results.append(
                    f"<tool_result>\nTool '{t_name}' execution output:\n{res}\n</tool_result>"
                )

            current_prompt = "\n".join(results)

        return last_reply or Reply(text="", conversation_id=self._conversation_id or "")

    def stream(
        self,
        message: str,
        thinking: Optional[bool] = None,
        search: Optional[bool] = None,
    ) -> Iterator[str]:
        prompt = self._prepare_prompt(message)
        use_thinking = self.thinking if thinking is None else thinking
        use_search = self.search if search is None else search

        model_type = self.model if self._conversation_id is None else None

        stream_obj = self.client.stream(
            prompt=prompt,
            conversation_id=self._conversation_id,
            model=model_type,
            thinking=use_thinking,
            search=use_search,
        )

        for chunk in stream_obj:
            yield chunk

        self._conversation_id = stream_obj.conversation_id

    def close(self) -> None:
        self.mcp_manager.close()
        self.client.close()


DeepSeekAgent = EDUAgent