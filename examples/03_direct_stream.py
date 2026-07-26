"""Example 3 — stream the reply as it is generated.

Run it from the project root:

    python examples/03_direct_stream.py
"""

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from eduagent import DeepSeekClient

client = DeepSeekClient()

stream = client.stream("Tell me a short, clean joke.")
for chunk in stream:
    print(chunk, end="", flush=True)

print()
print("conversation_id:", stream.conversation_id)
client.close()