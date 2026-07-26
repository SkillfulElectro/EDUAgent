"""Example 2 — a multi-turn conversation.

Run it from the project root:

    python examples/02_direct_conversation.py
"""

import sys
import pathlib
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from eduagent import DeepSeekClient

client = DeepSeekClient()

first = client.chat("My name is Ada. Remember it.")
print("DeepSeek:", first.text)
print("conversation_id:", first.conversation_id)

time.sleep(2)

second = client.chat("What's my name? Reply with just the name.",
                     conversation_id=first.conversation_id)
print("DeepSeek:", second.text)

client.close()