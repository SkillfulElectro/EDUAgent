"""Example 1 — the simplest chat turn.

Run it from the project root:

    python examples/01_direct_chat.py
"""

import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from eduagent import DeepSeekClient

client = DeepSeekClient()

reply = client.chat(
    "Say hello in one short sentence.",
    model="expert",
    thinking=True,
)
print(reply.text)
print("conversation_id:", reply.conversation_id)

client.close()