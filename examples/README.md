# EDUAgent Examples

Runnable examples demonstrating EDUAgent usage directly in Python. Run each from the **project root** (e.g. `python examples/01_direct_chat.py`).

| File | Description |
| --- | --- |
| [01_direct_chat.py](01_direct_chat.py) | One-shot chat turn using `DeepSeekClient` |
| [02_direct_conversation.py](02_direct_conversation.py) | Multi-turn chat using `conversation_id` |
| [03_direct_stream.py](03_direct_stream.py) | Real-time streaming response |
| [07_agent_single_chat.py](07_agent_single_chat.py) | Stateful `EDUAgent` maintaining thread context |
| [08_agent_with_mcp_and_tools.py](08_agent_with_mcp_and_tools.py) | `EDUAgent` running file tools and external MCP servers |