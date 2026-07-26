"""Pure-HTTP chat client for chat.deepseek.com with live event streaming support."""

from __future__ import annotations

import json
import os
import random
import re
import threading
import time
from dataclasses import dataclass
from typing import Iterator, Optional, Tuple, Union

import httpx

from .auth import Session, get_session
from .pow import DeepSeekPow

BASE = os.environ.get("DEEPSEEK_BASE_URL", "https://chat.deepseek.com")
COMPLETION_PATH = "/api/v0/chat/completion"

DEFAULT_MODEL_TYPE = "default"
_CID_SEP = ":"
TOOL_CALL_REGEX = re.compile(r"<?tool_call>.*?(?:</tool_call>|$)", re.DOTALL)


def _encode_cid(session_id: str, message_id: Optional[int]) -> str:
    if message_id is None:
        return session_id
    return f"{session_id}{_CID_SEP}{message_id}"


def _decode_cid(conversation_id: Optional[str]) -> tuple[Optional[str], Optional[int]]:
    if not conversation_id:
        return None, None
    session_id, _, msg = conversation_id.partition(_CID_SEP)
    parent = int(msg) if msg.isdigit() else None
    return (session_id or None), parent


@dataclass
class Reply:
    text: str
    conversation_id: str
    thinking: Optional[str] = None

    def __str__(self) -> str:
        return self.text


def _biz(data: dict) -> dict:
    if data.get("code") != 0:
        raise RuntimeError(f"DeepSeek API error: {data.get('msg') or data}")
    biz = data.get("data", {}).get("biz_data")
    if biz is None:
        raise RuntimeError(f"Unexpected response shape: {data}")
    return biz


def _clean_thinking_and_text(text: str, thinking: Optional[str]) -> tuple[str, Optional[str]]:
    clean_text = text or ""
    clean_thinking = thinking or ""

    if clean_thinking:
        tool_matches = TOOL_CALL_REGEX.findall(clean_thinking)
        if tool_matches:
            for tool_str in tool_matches:
                block = tool_str.strip()
                if block:
                    if block not in clean_text:
                        clean_text = (clean_text + "\n" + block).strip()
                    clean_thinking = clean_thinking.replace(tool_str, "").strip()

    return clean_text, (clean_thinking if clean_thinking else None)


class DeepSeekClient:
    def __init__(
        self,
        session: Optional[Session] = None,
        allow_interactive: bool = True,
        human_delay: bool = True,
        min_delay: float = 1.0,
        max_delay: float = 3.0,
    ):
        self.session = session or get_session(allow_interactive=allow_interactive)
        self.human_delay = human_delay
        self.min_delay = min_delay
        self.max_delay = max_delay
        self._pow = DeepSeekPow()
        self._pow_lock = threading.Lock()
        self._http = httpx.Client(
            base_url=BASE,
            headers=self._base_headers(),
            cookies=self.session.cookies,
            timeout=httpx.Timeout(
                connect=None,   # No connect timeout
                read=None,      # No read timeout
                write=None,     # No write timeout
                pool=None,      # No pool timeout
            ),
            transport=httpx.HTTPTransport(retries=3),  # Auto-retry on failures
        )

    def _base_headers(self) -> dict:
        return {
            "authorization": f"Bearer {self.session.token}",
            "accept": "*/*",
            "content-type": "application/json",
            "user-agent": self.session.user_agent,
            "origin": BASE,
            "referer": f"{BASE}/",
            "x-app-version": "2.0.0",
            "x-client-version": "2.0.0",
            "x-client-platform": "web",
            "x-client-locale": "en_US",
            "x-client-bundle-id": "com.deepseek.chat",
            "x-client-timezone-offset": "19800",
        }

    def create_chat_session(self) -> str:
        for attempt in range(3):
            try:
                r = self._http.post("/api/v0/chat_session/create", json={})
                r.raise_for_status()
                return _biz(r.json())["chat_session"]["id"]
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as e:
                if attempt == 2:
                    raise
                print(f"[client] Connection attempt {attempt+1} failed, retrying... ({e})")
                time.sleep(2 ** attempt)

    def _pow_header(self, target_path: str = COMPLETION_PATH) -> str:
        last_exc = None
        for attempt in range(5):
            try:
                r = self._http.post(
                    "/api/v0/chat/create_pow_challenge",
                    json={"target_path": target_path},
                )
                r.raise_for_status()
                challenge = _biz(r.json())["challenge"]
                with self._pow_lock:
                    return self._pow.make_header(challenge)
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as e:
                last_exc = e
                if attempt < 4:
                    wait = 2 ** attempt  # 1s, 2s, 4s, 8s
                    print(f"[client] PoW challenge request attempt {attempt+1} failed, "
                          f"retrying in {wait}s... ({e})")
                    time.sleep(wait)
        raise last_exc

    def stream(
        self,
        prompt: str,
        conversation_id: Optional[str] = None,
        model: Optional[str] = None,
        thinking: bool = False,
        search: bool = False,
        stream_events: bool = False,
    ) -> "_Stream":
        if conversation_id and model is not None:
            raise ValueError(
                "`model` cannot be set together with `conversation_id`; a thread's "
                "model is fixed when it is created. Pass `model` only on the first turn."
            )
        session_id, parent_id = _decode_cid(conversation_id)
        if session_id is None:
            session_id = self.create_chat_session()
            model_type: Optional[str] = model or DEFAULT_MODEL_TYPE
        else:
            model_type = None
        return _Stream(self, prompt, session_id, parent_id, model_type, thinking, search, stream_events)

    def chat(
        self,
        prompt: str,
        conversation_id: Optional[str] = None,
        model: Optional[str] = None,
        thinking: bool = False,
        search: bool = False,
    ) -> Reply:
        s = self.stream(prompt, conversation_id=conversation_id,
                        model=model, thinking=thinking, search=search)
        text = "".join(str(chunk) for chunk in s)

        clean_text, clean_thinking = _clean_thinking_and_text(text, s.thinking)
        return Reply(text=clean_text, conversation_id=s.conversation_id, thinking=clean_thinking)

    def close(self) -> None:
        self._http.close()


class _Stream:
    def __init__(
        self,
        client: "DeepSeekClient",
        prompt: str,
        session_id: str,
        parent_id: Optional[int],
        model: Optional[str],
        thinking: bool,
        search: bool,
        stream_events: bool = False,
    ):
        self._client = client
        self._prompt = prompt
        self._session_id = session_id
        self._parent_id = parent_id
        self._model = model
        self._thinking_enabled = thinking
        self._search_enabled = search
        self._stream_events = stream_events
        self._message_id: Optional[int] = None
        self._thinking_text: Optional[str] = None

    def __iter__(self) -> Iterator[Union[str, Tuple[str, str]]]:
        if self._client.human_delay:
            delay = random.uniform(self._client.min_delay, self._client.max_delay)
            time.sleep(delay)

        body = {
            "chat_session_id": self._session_id,
            "parent_message_id": self._parent_id,
            "prompt": self._prompt,
            "ref_file_ids": [],
            "thinking_enabled": self._thinking_enabled,
            "search_enabled": self._search_enabled,
            "action": None,
            "preempt": False,
        }
        if self._model is not None:
            body["model_type"] = self._model
        headers = {"x-ds-pow-response": self._client._pow_header()}
        meta: dict = {}
        with self._client._http.stream(
            "POST", COMPLETION_PATH, json=body, headers=headers
        ) as resp:
            resp.raise_for_status()
            yield from _parse_sse(resp.iter_lines(), meta, stream_events=self._stream_events)
        if meta.get("message_id") is not None:
            self._message_id = meta["message_id"]
        if meta.get("thinking"):
            self._thinking_text = meta["thinking"]

    @property
    def conversation_id(self) -> str:
        return _encode_cid(self._session_id, self._message_id)

    @property
    def thinking(self) -> Optional[str]:
        return self._thinking_text


def _parse_sse(
    lines, meta: Optional[dict] = None, stream_events: bool = False
) -> Iterator[Union[str, Tuple[str, str]]]:
    active_path: Optional[str] = None
    emitted_initial = False
    fragment_types = []
    thinking_chunks = []

    for line in lines:
        if not line or not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue

        v = obj.get("v")

        if isinstance(v, dict) and "response" in v:
            if meta is not None:
                _capture_message_id(meta, v)
            resp_obj = v.get("response", {})
            frags = resp_obj.get("fragments", [])
            fragment_types = [f.get("type") for f in frags]

            for idx, frag in enumerate(frags):
                ftype = frag.get("type")
                fcontent = frag.get("content", "")
                if ftype == "RESPONSE":
                    active_path = f"response/fragments/{idx}/content"
                    if fcontent and not emitted_initial:
                        emitted_initial = True
                        yield ("text", fcontent) if stream_events else fcontent
                elif ftype == "THINK":
                    if fcontent:
                        thinking_chunks.append(fcontent)
                        if stream_events:
                            yield ("think", fcontent)
            continue

        if "p" in obj:
            active_path = obj["p"]
            if meta is not None and active_path.endswith("message_id") and isinstance(v, int):
                meta["message_id"] = v

            if isinstance(v, dict) and "type" in v and "fragments" in active_path:
                ftype = v.get("type")
                parts = active_path.split("/")
                try:
                    idx = int(parts[parts.index("fragments") + 1])
                    while len(fragment_types) <= idx:
                        fragment_types.append(None)
                    fragment_types[idx] = ftype
                except (ValueError, IndexError):
                    pass

        if isinstance(v, str) and active_path and active_path.endswith("content"):
            frag_type = None
            if "fragments/" in active_path:
                parts = active_path.split("/")
                try:
                    idx_str = parts[parts.index("fragments") + 1]
                    idx = int(idx_str)
                    if 0 <= idx < len(fragment_types):
                        frag_type = fragment_types[idx]
                    elif idx == -1 and fragment_types:
                        frag_type = fragment_types[-1]
                    elif idx > 0:
                        frag_type = "RESPONSE"
                except (ValueError, IndexError):
                    pass

            if frag_type == "RESPONSE" or (frag_type is None and not fragment_types):
                yield ("text", v) if stream_events else v
            elif frag_type == "THINK":
                thinking_chunks.append(v)
                if stream_events:
                    yield ("think", v)

    if meta is not None and thinking_chunks:
        meta["thinking"] = "".join(thinking_chunks)


def _capture_message_id(meta: dict, snapshot: dict) -> None:
    for container in (snapshot.get("response"), snapshot):
        if isinstance(container, dict):
            mid = container.get("message_id", container.get("id"))
            if isinstance(mid, int):
                meta["message_id"] = mid
                return