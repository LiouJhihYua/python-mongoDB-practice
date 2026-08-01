"""SECS-II / HSMS 協定編解碼（SEMI E5 / E37）。

這一層完全不碰資料庫，只做「位元組 ↔ Python 結構」的轉換，
因此可以單獨測試，也能被設備模擬器重複使用。

SECS-II 的項目（Item）在這裡一律表示成可直接存進 JSONB 的字典::

    {"format": "L", "value": [
        {"format": "A",  "value": "MES"},
        {"format": "U4", "value": [1]},
    ]}

HSMS 訊息則是 4 位元組長度 + 10 位元組表頭 + SECS-II 內容：

===== ==================================================
位元組 內容
===== ==================================================
0-1   Session ID（設備 ID）
2     資料訊息為 W-bit + Stream；控制訊息另有定義
3     Function
4     PType（0 = SECS-II）
5     SType（0 = 資料訊息，其餘為控制訊息）
6-9   System Bytes（交易序號，用來配對請求與回覆）
===== ==================================================
"""

from __future__ import annotations

import struct
from typing import Any

# ── 項目格式代碼（SEMI E5，格式碼佔表頭位元組的高 6 位元）──
FORMAT_CODES: dict[int, str] = {
    0o00: "L",        # List
    0o10: "B",        # Binary
    0o11: "BOOLEAN",
    0o20: "A",        # ASCII
    0o21: "J",        # JIS-8
    0o30: "I8",
    0o31: "I1",
    0o32: "I2",
    0o34: "I4",
    0o40: "F8",
    0o44: "F4",
    0o50: "U8",
    0o51: "U1",
    0o52: "U2",
    0o54: "U4",
}
CODE_BY_FORMAT: dict[str, int] = {v: k for k, v in FORMAT_CODES.items()}

#: 數值型項目的 struct 格式與位元組寬度
NUMERIC: dict[str, tuple[str, int]] = {
    "I1": (">b", 1), "I2": (">h", 2), "I4": (">i", 4), "I8": (">q", 8),
    "U1": (">B", 1), "U2": (">H", 2), "U4": (">I", 4), "U8": (">Q", 8),
    "F4": (">f", 4), "F8": (">d", 8),
}

#: HSMS SType（控制訊息類別）
ST_DATA = 0
ST_SELECT_REQ = 1
ST_SELECT_RSP = 2
ST_DESELECT_REQ = 3
ST_DESELECT_RSP = 4
ST_LINKTEST_REQ = 5
ST_LINKTEST_RSP = 6
ST_REJECT_REQ = 7
ST_SEPARATE_REQ = 9

STYPE_NAMES = {
    ST_DATA: "DATA", ST_SELECT_REQ: "SELECT.REQ", ST_SELECT_RSP: "SELECT.RSP",
    ST_DESELECT_REQ: "DESELECT.REQ", ST_DESELECT_RSP: "DESELECT.RSP",
    ST_LINKTEST_REQ: "LINKTEST.REQ", ST_LINKTEST_RSP: "LINKTEST.RSP",
    ST_REJECT_REQ: "REJECT.REQ", ST_SEPARATE_REQ: "SEPARATE.REQ",
}

HEADER_SIZE = 10
#: 單一訊息長度上限，避免壞掉的對端塞爆記憶體
MAX_MESSAGE_BYTES = 8 * 1024 * 1024


class SECSError(Exception):
    """協定層錯誤（格式不合、長度不符…）。"""


# ── 項目建構捷徑 ────────────────────────────────────────────
def L(*items: dict) -> dict:
    return {"format": "L", "value": list(items)}


def A(text: str) -> dict:
    return {"format": "A", "value": text}


def B(*values: int) -> dict:
    return {"format": "B", "value": list(values)}


def BOOLEAN(*values: bool) -> dict:
    return {"format": "BOOLEAN", "value": list(values)}


def _numeric(fmt: str):
    def build(*values: Any) -> dict:
        return {"format": fmt, "value": list(values)}

    build.__name__ = fmt
    return build


U1, U2, U4, U8 = _numeric("U1"), _numeric("U2"), _numeric("U4"), _numeric("U8")
I1, I2, I4, I8 = _numeric("I1"), _numeric("I2"), _numeric("I4"), _numeric("I8")
F4, F8 = _numeric("F4"), _numeric("F8")


# ── 項目編碼 ────────────────────────────────────────────────
def _length_bytes(length: int) -> bytes:
    if length < 0:
        raise SECSError("項目長度不可為負")
    if length <= 0xFF:
        return bytes([length])
    if length <= 0xFFFF:
        return struct.pack(">H", length)
    if length <= 0xFFFFFF:
        return struct.pack(">I", length)[1:]
    raise SECSError(f"項目長度 {length} 超過 SECS-II 上限（3 位元組）")


def encode_item(item: dict) -> bytes:
    """把單一項目（含其子項目）編成位元組。"""
    fmt = str(item.get("format", "")).upper()
    if fmt not in CODE_BY_FORMAT:
        raise SECSError(f"不支援的項目格式：{item.get('format')!r}")
    value = item.get("value")

    if fmt == "L":
        children = list(value or [])
        payload = b"".join(encode_item(child) for child in children)
        count = len(children)
    elif fmt in ("A", "J"):
        payload = str(value if value is not None else "").encode("latin-1", errors="replace")
        count = len(payload)
    elif fmt == "B":
        raw = value if isinstance(value, (bytes, bytearray)) else bytes(int(v) & 0xFF for v in (value or []))
        payload = bytes(raw)
        count = len(payload)
    elif fmt == "BOOLEAN":
        values = value if isinstance(value, (list, tuple)) else [value]
        payload = bytes(1 if v else 0 for v in values)
        count = len(payload)
    else:
        pack, width = NUMERIC[fmt]
        values = value if isinstance(value, (list, tuple)) else [value]
        try:
            payload = b"".join(struct.pack(pack, v) for v in values)
        except struct.error as exc:
            raise SECSError(f"{fmt} 項目的值超出範圍：{value!r}（{exc}）")
        count = len(payload)

    size = _length_bytes(count)
    header = bytes([(CODE_BY_FORMAT[fmt] << 2) | len(size)])
    return header + size + payload


def decode_item(data: bytes, offset: int = 0) -> tuple[dict, int]:
    """自 ``offset`` 解出一個項目，回傳 (項目, 下一個起點)。"""
    if offset >= len(data):
        raise SECSError("項目資料不足，無法讀取表頭")
    header = data[offset]
    code, size_len = header >> 2, header & 0b11
    if size_len == 0:
        raise SECSError("項目長度位元組數不可為 0")
    fmt = FORMAT_CODES.get(code)
    if fmt is None:
        raise SECSError(f"未知的項目格式代碼：{code}")

    start = offset + 1
    if start + size_len > len(data):
        raise SECSError("項目資料不足，無法讀取長度")
    length = int.from_bytes(data[start: start + size_len], "big")
    body = start + size_len
    if fmt != "L" and body + length > len(data):
        raise SECSError(f"項目長度 {length} 超過剩餘資料")

    if fmt == "L":
        children: list[dict] = []
        cursor = body
        for _ in range(length):
            child, cursor = decode_item(data, cursor)
            children.append(child)
        return {"format": "L", "value": children}, cursor

    chunk = data[body: body + length]
    if fmt in ("A", "J"):
        return {"format": fmt, "value": chunk.decode("latin-1")}, body + length
    if fmt == "B":
        return {"format": "B", "value": list(chunk)}, body + length
    if fmt == "BOOLEAN":
        return {"format": "BOOLEAN", "value": [bool(b) for b in chunk]}, body + length

    pack, width = NUMERIC[fmt]
    if length % width:
        raise SECSError(f"{fmt} 項目長度 {length} 不是 {width} 的倍數")
    values = [struct.unpack(pack, chunk[i: i + width])[0] for i in range(0, length, width)]
    return {"format": fmt, "value": values}, body + length


def to_python(item: dict | None) -> Any:
    """把項目轉成單純的 Python 值，方便業務邏輯取用。"""
    if item is None:
        return None
    fmt = str(item.get("format", "")).upper()
    value = item.get("value")
    if fmt == "L":
        return [to_python(child) for child in (value or [])]
    if fmt in ("A", "J"):
        return value
    if isinstance(value, (list, tuple)):
        return list(value)[0] if len(value) == 1 else list(value)
    return value


def to_sml(item: dict | None, indent: int = 0) -> str:
    """轉成 SML 文字，訊息記錄與除錯用。"""
    if item is None:
        return "<>"
    pad = "  " * indent
    fmt = str(item.get("format", "")).upper()
    value = item.get("value")
    if fmt == "L":
        children = list(value or [])
        if not children:
            return f"{pad}<L [0]>"
        inner = "\n".join(to_sml(child, indent + 1) for child in children)
        return f"{pad}<L [{len(children)}]\n{inner}\n{pad}>"
    if fmt in ("A", "J"):
        return f'{pad}<{fmt} "{value}">'
    values = value if isinstance(value, (list, tuple)) else [value]
    return f"{pad}<{fmt} {' '.join(str(v) for v in values)}>"


# ── HSMS 訊息 ───────────────────────────────────────────────
def encode_message(
    session_id: int,
    stream: int,
    function: int,
    w_bit: bool = False,
    system_bytes: int = 0,
    body: dict | None = None,
    stype: int = ST_DATA,
) -> bytes:
    """組出完整的 HSMS 位元組（含 4 位元組長度前綴）。"""
    if stype == ST_DATA:
        byte2 = (0x80 if w_bit else 0x00) | (stream & 0x7F)
        byte3 = function & 0xFF
    else:
        # 控制訊息不帶 Stream / Function；Select.rsp 等以 byte3 傳回應碼
        byte2 = stream & 0xFF
        byte3 = function & 0xFF
    header = struct.pack(
        ">HBBBBI", session_id & 0xFFFF, byte2, byte3, 0, stype & 0xFF, system_bytes & 0xFFFFFFFF
    )
    payload = encode_item(body) if body else b""
    frame = header + payload
    return struct.pack(">I", len(frame)) + frame


def decode_message(frame: bytes) -> dict:
    """解出 HSMS 訊息（``frame`` 不含 4 位元組長度前綴）。"""
    if len(frame) < HEADER_SIZE:
        raise SECSError(f"HSMS 訊息長度 {len(frame)} 不足 {HEADER_SIZE} 位元組表頭")
    session_id, byte2, byte3, ptype, stype, system_bytes = struct.unpack(
        ">HBBBBI", frame[:HEADER_SIZE]
    )
    message: dict[str, Any] = {
        "session_id": session_id,
        "ptype": ptype,
        "stype": stype,
        "stype_name": STYPE_NAMES.get(stype, f"STYPE_{stype}"),
        "system_bytes": system_bytes,
        "w_bit": bool(byte2 & 0x80) if stype == ST_DATA else False,
        "stream": byte2 & 0x7F if stype == ST_DATA else byte2,
        "function": byte3,
        "body": None,
    }
    payload = frame[HEADER_SIZE:]
    if stype == ST_DATA and payload:
        item, consumed = decode_item(payload, 0)
        if consumed != len(payload):
            raise SECSError(f"訊息內容剩餘 {len(payload) - consumed} 位元組未解析")
        message["body"] = item
    return message


def message_name(message: dict) -> str:
    """人看得懂的訊息名稱，例如 ``S6F11 W`` 或 ``LINKTEST.REQ``。"""
    if message.get("stype", ST_DATA) != ST_DATA:
        return STYPE_NAMES.get(message["stype"], f"STYPE_{message['stype']}")
    return f"S{message['stream']}F{message['function']}" + (" W" if message.get("w_bit") else "")


def split_frames(buffer: bytes) -> tuple[list[bytes], bytes]:
    """自 TCP 串流切出完整訊息，回傳 (訊息串列, 尚未收完的殘料)。"""
    frames: list[bytes] = []
    cursor = 0
    while len(buffer) - cursor >= 4:
        (length,) = struct.unpack(">I", buffer[cursor: cursor + 4])
        if length > MAX_MESSAGE_BYTES:
            raise SECSError(f"HSMS 訊息長度 {length} 超過上限")
        if len(buffer) - cursor - 4 < length:
            break
        frames.append(buffer[cursor + 4: cursor + 4 + length])
        cursor += 4 + length
    return frames, buffer[cursor:]
