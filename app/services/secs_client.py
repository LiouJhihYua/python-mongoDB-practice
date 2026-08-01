"""HSMS-SS 用戶端（SEMI E37）。

MES 端一律採 ACTIVE 模式：由本系統主動連向設備的 HSMS 埠，
建立 TCP 之後送 Select.req 進入 SELECTED 狀態才開始收發資料訊息。

實作的計時器與標準一致：

* **T3**（回覆逾時）—— 送出帶 W-bit 的訊息後等回覆的上限
* **T5**（連線分離）—— 斷線後隔多久重連
* **T6**（控制交易）—— Select / Linktest 的回覆上限
* **Linktest** —— 閒置時定期送 Linktest.req 確認線路還活著

連線與重連都在背景任務裡跑，斷線不會讓 API 掛掉；
設備狀態變化透過 ``on_state`` 回呼寫回資料庫。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from app.services import secs

logger = logging.getLogger("mes.secs")

MessageHandler = Callable[["HsmsClient", dict], Awaitable[None]]
StateHandler = Callable[["HsmsClient", str, str], Awaitable[None]]


class HsmsClient:
    """單一設備的 HSMS 連線。"""

    def __init__(
        self,
        eq_id: str,
        host: str,
        port: int,
        session_id: int = 0,
        t3_timeout_sec: int = 45,
        t5_timeout_sec: int = 10,
        t6_timeout_sec: int = 5,
        linktest_sec: int = 30,
        on_message: MessageHandler | None = None,
        on_state: StateHandler | None = None,
    ):
        self.eq_id = eq_id
        self.host = host
        self.port = port
        self.session_id = session_id
        self.t3 = t3_timeout_sec
        self.t5 = t5_timeout_sec
        self.t6 = t6_timeout_sec
        self.linktest_interval = linktest_sec
        self.on_message = on_message
        self.on_state = on_state

        self.state = "NOT_CONNECTED"
        self.last_error: str | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._tasks: list[asyncio.Task] = []
        self._pending: dict[int, asyncio.Future] = {}
        self._system_bytes = 0
        self._running = False
        self._send_lock = asyncio.Lock()

    # ── 狀態 ────────────────────────────────────────────────
    async def _set_state(self, state: str, error: str | None = None) -> None:
        if state == self.state and error == self.last_error:
            return
        self.state = state
        self.last_error = error
        logger.info("設備 %s HSMS 狀態 → %s%s", self.eq_id, state, f"（{error}）" if error else "")
        if self.on_state is not None:
            try:
                await self.on_state(self, state, error or "")
            except Exception as exc:  # 狀態回寫失敗不應該打斷連線
                logger.warning("設備 %s 狀態回寫失敗：%s", self.eq_id, exc)

    @property
    def selected(self) -> bool:
        return self.state == "SELECTED"

    def _next_system_bytes(self) -> int:
        self._system_bytes = (self._system_bytes + 1) & 0xFFFFFFFF
        return self._system_bytes

    # ── 連線管理 ────────────────────────────────────────────
    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._tasks.append(asyncio.create_task(self._connect_loop(), name=f"hsms-{self.eq_id}"))

    async def stop(self) -> None:
        self._running = False
        await self._teardown("主動停止")
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()
        await self._set_state("NOT_CONNECTED")

    async def _connect_loop(self) -> None:
        while self._running:
            try:
                await self._connect_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._set_state("DISCONNECTED", str(exc))
            if not self._running:
                break
            await asyncio.sleep(max(1, self.t5))

    async def _connect_once(self) -> None:
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), timeout=max(1, self.t5)
        )
        await self._set_state("CONNECTED")
        reader_task = asyncio.create_task(self._read_loop(), name=f"hsms-read-{self.eq_id}")
        try:
            await self._select()
            linktest_task = asyncio.create_task(
                self._linktest_loop(), name=f"hsms-linktest-{self.eq_id}"
            )
            try:
                await reader_task
            finally:
                linktest_task.cancel()
        finally:
            reader_task.cancel()
            await self._teardown("連線結束")

    async def _teardown(self, reason: str) -> None:
        writer, self._writer, self._reader = self._writer, None, None
        if writer is not None:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
        for future in self._pending.values():
            if not future.done():
                future.set_exception(ConnectionError(f"連線中斷：{reason}"))
        self._pending.clear()

    async def _select(self) -> None:
        frame = secs.encode_message(
            self.session_id, 0, 0, False, self._next_system_bytes(), None, secs.ST_SELECT_REQ
        )
        future = self._register(self._system_bytes)
        await self._write(frame)
        reply = await asyncio.wait_for(future, timeout=max(1, self.t6))
        if reply.get("function", 0) != 0:
            raise ConnectionError(f"Select 被拒絕，回應碼 {reply.get('function')}")
        await self._set_state("SELECTED")

    # ── 收送 ────────────────────────────────────────────────
    def _register(self, system_bytes: int) -> asyncio.Future:
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[system_bytes] = future
        return future

    async def _write(self, frame: bytes) -> None:
        if self._writer is None:
            raise ConnectionError(f"設備 {self.eq_id} 尚未連線")
        async with self._send_lock:
            self._writer.write(frame)
            await self._writer.drain()

    async def send(
        self, stream: int, function: int, body: dict | None = None,
        w_bit: bool = False, system_bytes: int | None = None,
    ) -> int:
        """送出資料訊息，回傳這次交易的 System Bytes。"""
        sb = system_bytes if system_bytes is not None else self._next_system_bytes()
        await self._write(
            secs.encode_message(self.session_id, stream, function, w_bit, sb, body, secs.ST_DATA)
        )
        return sb

    async def request(
        self, stream: int, function: int, body: dict | None = None, timeout: float | None = None
    ) -> dict:
        """送出帶 W-bit 的訊息並等對方回覆（T3 逾時）。"""
        if not self.selected:
            raise ConnectionError(f"設備 {self.eq_id} 尚未進入 SELECTED 狀態")
        sb = self._next_system_bytes()
        future = self._register(sb)
        try:
            await self.send(stream, function, body, w_bit=True, system_bytes=sb)
            return await asyncio.wait_for(future, timeout=timeout or self.t3)
        except asyncio.TimeoutError:
            self._pending.pop(sb, None)
            raise TimeoutError(f"設備 {self.eq_id} 的 S{stream}F{function} 在 {timeout or self.t3} 秒內未回覆")
        finally:
            self._pending.pop(sb, None)

    async def reply(self, request: dict, function: int, body: dict | None = None) -> None:
        """以相同的 System Bytes 回覆對方的請求。"""
        await self.send(
            request["stream"], function, body, w_bit=False, system_bytes=request["system_bytes"]
        )

    async def _read_loop(self) -> None:
        buffer = b""
        assert self._reader is not None
        while True:
            chunk = await self._reader.read(65536)
            if not chunk:
                raise ConnectionError("對端關閉連線")
            buffer += chunk
            frames, buffer = secs.split_frames(buffer)
            for frame in frames:
                try:
                    message = secs.decode_message(frame)
                except secs.SECSError as exc:
                    logger.warning("設備 %s 送來無法解析的訊息：%s", self.eq_id, exc)
                    continue
                await self._dispatch(message)

    async def _dispatch(self, message: dict) -> None:
        stype = message["stype"]
        if stype == secs.ST_LINKTEST_REQ:
            await self._write(
                secs.encode_message(
                    self.session_id, 0, 0, False, message["system_bytes"], None, secs.ST_LINKTEST_RSP
                )
            )
            return
        if stype in (secs.ST_SELECT_RSP, secs.ST_LINKTEST_RSP, secs.ST_DESELECT_RSP):
            future = self._pending.pop(message["system_bytes"], None)
            if future is not None and not future.done():
                future.set_result(message)
            return
        if stype == secs.ST_SEPARATE_REQ:
            raise ConnectionError("對端要求 Separate")
        if stype != secs.ST_DATA:
            return

        future = self._pending.pop(message["system_bytes"], None)
        if future is not None and not future.done():
            future.set_result(message)
            return
        if self.on_message is not None:
            try:
                await self.on_message(self, message)
            except Exception as exc:
                logger.warning("設備 %s 的訊息處理失敗：%s", self.eq_id, exc)

    async def _linktest_loop(self) -> None:
        while True:
            await asyncio.sleep(max(5, self.linktest_interval))
            sb = self._next_system_bytes()
            future = self._register(sb)
            try:
                await self._write(
                    secs.encode_message(self.session_id, 0, 0, False, sb, None, secs.ST_LINKTEST_REQ)
                )
                await asyncio.wait_for(future, timeout=max(1, self.t6))
            except Exception as exc:
                self._pending.pop(sb, None)
                logger.warning("設備 %s Linktest 失敗：%s", self.eq_id, exc)
                raise

    def info(self) -> dict[str, Any]:
        return {
            "eq_id": self.eq_id,
            "host": self.host,
            "port": self.port,
            "session_id": self.session_id,
            "state": self.state,
            "last_error": self.last_error,
            "pending_transactions": len(self._pending),
        }
