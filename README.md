# EDUAgent

The project goal is to visualize how tool calls and mcp works under the hood in the chat interface DeepSeek. so its like running an Agent and user can open their deepseek chat interface and see what really happened under the hood.

By driving the actual DeepSeek chat session, EDUAgent lets you execute local sandboxed file tools, shell commands, and remote Model Context Protocol (MCP) tools while keeping the complete chat thread, reasoning process, tool call payloads (`<tool_call>`), and tool outputs (`<tool_result>`) visible directly in your DeepSeek web UI.

---

## Features

- **DeepSeek Web Interface Transparency**: Open [chat.deepseek.com](https://chat.deepseek.com) to see the exact prompts, tool call JSON payloads, and execution results formatted live in the official web interface.
- **Single Entrypoint**: Simply run `python main.py`. It verifies your login (launching a browser window if sign-in is required) and interactively prompts for workspace, thinking mode, search mode, shell policy, and human delay settings.
- **Real-Time Live Response Streaming**: Streams reasoning (`🧠 Thinking:`) and model output live chunk-by-chunk in real time.
- **Clean Tool Argument Summarization**: Summarizes large file write contents (e.g. `write_file(path='app.py', content='<45 lines, 1200 chars>')`) so long code blocks do not clutter your terminal logs.
- **Agentic Line-Specific File Reading**: Read precise line ranges (`start_line`, `end_line`) from files to inspect large source files efficiently.
- **Safe Shell Tool Interaction**: Execute shell commands with customizable security policies:
  1. **Manual Accept**: Prompt before running any command.
  2. **Auto Accept**: Run all commands automatically.
  3. **Auto Safe, Reject Unsafe**: Auto-run safe commands; automatically reject dangerous commands.
  4. **Auto Safe, Manual Unsafe**: Auto-run safe commands; prompt user for manual approval when high-risk commands are detected.
- **Multiline Input Support**: Paste or type multi-line code blocks, logs, or messages directly in the CLI. Press Enter on an empty line or type `/end` to finish input.
- **Customizable Request Pacing**: Configure human-like request delays (`min_delay` and `max_delay`) or disable delays entirely.
- **MCP Server Configuration**: Add stdio MCP server configurations to `mcp_servers.json` to expose custom tools.

---

## Quick Start

### 1. Installation

Install required Python dependencies and Playwright browser binaries:

```bash
pip install -r requirements.txt
playwright install chromium
```
### 2. Single Entrypoint Execution

Run the single entrypoint:
```bash
python main.py
```
At startup, EDUAgent will check your authentication status and present interactive setup prompts:

```txt
⚙️  EDUAgent Launch Setup
--------------------------------------------------
📁 Workspace directory [default: ./workspace]:
🤖 Select DeepSeek Model (1: Instant / 2: Expert):
🧠 Enable DeepThink reasoning mode? (y/n):
🌐 Enable web search mode? (y/n):
🛡️  Select Shell Command Execution Policy:
  [1] Manual Accept
  [2] Auto Accept
  [3] Auto Safe, Reject Unsafe
  [4] Auto Safe, Manual Unsafe (default)
⏱️  Human-like Request Pacing Delay:
  Enable randomized delay before requests? (y/n):
  Enter delay range in seconds (min-max) [default: 1.0-3.0]:
```
### 3. Chat Interaction

Once the agent is active, you can chat with it interactively. To send **multiline messages** (e.g., code snippets, logs, structured text), simply continue typing after the first line:

```txt
You: Explain this function:
...  def greet(name):
...      return f"Hello, {name}!"
...  
```
Press Enter on an **empty line** (or type `/end`) to submit. The entire multiline block is sent as one message.

Single-line messages work as before — just press Enter once after your input.

## Configuring MCP Servers (mcp_servers.json)

To connect external Model Context Protocol (MCP) servers, add them to mcp_servers.json using the standard JSON format:
code JSON
```json
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": [
        "-y",
        "@modelcontextprotocol/server-filesystem",
        "./workspace"
      ]
    },
    "fetch": {
      "command": "uvx",
      "args": ["mcp-server-fetch"]
    }
  }
}
```
When EDUAgent launches, it automatically connects to all configured MCP servers and registers their tools.

## License
GPL-3.0
