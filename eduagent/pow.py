"""
Proof-of-work solver for DeepSeek's chat completion endpoint.
Runs DeepSeek's Sha3 WASM module inside wasmtime.
"""

from __future__ import annotations

import base64
import json
import struct
from pathlib import Path
from typing import Optional

import wasmtime

WASM_PATH = Path(__file__).resolve().parent / "sha3_wasm_bg.wasm"


class DeepSeekPow:
    def __init__(self, wasm_path: Path = WASM_PATH):
        self._store = wasmtime.Store()
        module = wasmtime.Module.from_file(self._store.engine, str(wasm_path))
        self._inst = wasmtime.Instance(self._store, module, [])
        exp = self._inst.exports(self._store)
        self._memory: wasmtime.Memory = exp["memory"]
        self._solve = exp["wasm_solve"]
        self._malloc = exp["__wbindgen_export_0"]
        self._add_to_stack = exp["__wbindgen_add_to_stack_pointer"]

    def _write_str(self, text: str) -> tuple[int, int]:
        data = text.encode("utf-8")
        ptr = self._malloc(self._store, len(data), 1)
        base = self._memory.data_ptr(self._store)
        for i, b in enumerate(data):
            base[ptr + i] = b
        return ptr, len(data)

    def solve(self, challenge: str, prefix: str, difficulty: float) -> Optional[int]:
        retptr = self._add_to_stack(self._store, -16)
        try:
            c_ptr, c_len = self._write_str(challenge)
            p_ptr, p_len = self._write_str(prefix)
            self._solve(self._store, retptr, c_ptr, c_len, p_ptr, p_len, float(difficulty))

            mem = self._memory.data_ptr(self._store)
            status = struct.unpack("<i", bytes(mem[retptr:retptr + 4]))[0]
            value = struct.unpack("<d", bytes(mem[retptr + 8:retptr + 16]))[0]
        finally:
            self._add_to_stack(self._store, 16)

        if status == 0:
            return None
        return int(value)

    def make_header(self, challenge: dict) -> str:
        prefix = f"{challenge['salt']}_{challenge['expire_at']}_"
        answer = self.solve(challenge["challenge"], prefix, challenge["difficulty"])
        if answer is None:
            raise RuntimeError("PoW solver returned no answer (challenge expired?)")
        payload = {
            "algorithm": challenge["algorithm"],
            "challenge": challenge["challenge"],
            "salt": challenge["salt"],
            "answer": answer,
            "signature": challenge["signature"],
            "target_path": challenge["target_path"],
        }
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        return base64.b64encode(raw).decode("utf-8")