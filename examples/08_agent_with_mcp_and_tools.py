"""Example 8 — EDUAgent with Line-Range File Tools, Safe Shell Interaction & MCP Support.

Run it from the project root:

    python examples/08_agent_with_mcp_and_tools.py
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from eduagent import EDUAgent

WORK_DIR = pathlib.Path("./workspace")

agent = EDUAgent(
    work_dir=WORK_DIR,
    model="default",
    shell_policy="auto_safe_manual_unsafe",
    system_prompt="You are an automated coding agent.",
    mcp_config_path="mcp_servers.json",
)

print("--- Agent Writing File ---")
reply1 = agent.chat("Create a python script named 'calculator.py' with 20 lines of helper code.")
print(reply1.text)

print("\n--- Agent Reading Specific Lines ---")
reply2 = agent.chat("Read lines 5 through 12 of 'calculator.py'.")
print(reply2.text)

print("\n--- Agent Running Shell Command ---")
reply3 = agent.chat("Run python --version in the terminal.")
print(reply3.text)

agent.close()