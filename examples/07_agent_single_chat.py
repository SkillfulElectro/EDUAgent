"""Example 7 — Stateful EDUAgent managing a single chat room on DeepSeek.

Run it from the project root:

    python examples/07_agent_single_chat.py
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from eduagent import EDUAgent

agent = EDUAgent(
    model="expert",
    thinking=True,
    system_prompt="You are a concise python expert.",
)

print("--- Starting Chat Room 1 ---")
reply1 = agent.chat("My favorite language is Python. Remember that.")
print("DeepSeek:", reply1.text)

reply2 = agent.chat("What is my favorite language?")
print("DeepSeek:", reply2.text)

print("\nCurrent conversation ID:", agent.conversation_id)

print("\n--- Moving into Chat Room 2 ---")
agent.new_chat(model="default")

reply3 = agent.chat("What is my favorite language?")
print("DeepSeek:", reply3.text)

agent.close()