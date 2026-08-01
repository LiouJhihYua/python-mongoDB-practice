"""SECS/GEM 設備模擬器（HSMS-SS Passive）。

導入 MES 時最麻煩的一件事，是機台還沒接上就沒東西可測。
這支模擬器扮演一台會說 SECS/GEM 的機台：開一個 HSMS 埠等 MES 連進來，
回覆常見的 GEM 訊息，並週期性送出事件報告（S6F11）與警報（S5F1）。

單獨執行::

    python -m scripts.eqsim --port 5000 --eq-id WB-01

也可以在程式裡當成物件用（測試就是這樣做的）::

    sim = EquipmentSimulator(port=0)
    port = await sim.start()
    ...
    await sim.stop()
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import random
from typing import Any

from app.services import secs

logger = logging.getLogger("eqsim")

#: 模擬器內建的事件代碼，與 scripts/seed.py 建立的規則對應
CEID_PROCESS_START = 2001
CEID_PROCESS_END = 2002
CEID_STATE_CHANGE = 2003


class EquipmentSimulator:
    """一台會說 HSMS + GEM 的假機台。"""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 5000,
        session_id: int = 0,
        model: str = "SIM-BONDER",
        softrev: str = "1.0.0",
        auto_event_sec: float = 0.0,
    ):
        self.host = host
        self.port = port
        self.session_id = session_id
        self.model = model
        self.softrev = softrev
        self.auto_event_sec = auto_event_sec

        self.state = "IDLE"
        self.selected = False
        self.received: list[dict] = []
        self._server: asyncio.AbstractServer | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._system_bytes = 0x8000_0000
        self._auto_task: asyncio.Task | None = None

    # ── 生命週期 ────────────────────────────────────────────
    async def start(self) -> int:
        self._server = await asyncio.start_server(self._handle_client, self.host, self.port)
        self.port = self._server.sockets[0].getsockname()[1]
        logger.info("設備模擬器在 %s:%d 等待 MES 連線", self.host, self.port)
        if self.auto_event_sec > 0:
            self._auto_task = asyncio.create_task(self._auto_loop(), name="eqsim-auto")
        return self.port

    async def stop(self) -> None:
        if self._auto_task is not None:
            self._auto_task.cancel()
            try:
                await self._auto_task
            except (asyncio.CancelledError, Exception):
                pass
            self._auto_task = None
        if self._writer is not None:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
            self._writer = None
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        self.selected = False

    def _next_system_bytes(self) -> int:
        self._system_bytes = (self._system_bytes + 1) & 0xFFFFFFFF
        return self._system_bytes

    # ── 連線處理 ────────────────────────────────────────────
    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        logger.info("MES 已連入：%s", peer)
        self._writer = writer
        buffer = b""
        try:
            while True:
                chunk = await reader.read(65536)
                if not chunk:
                    break
                buffer += chunk
                frames, buffer = secs.split_frames(buffer)
                for frame in frames:
                    await self._on_frame(secs.decode_message(frame))
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        except Exception as exc:
            logger.warning("連線處理失敗：%s", exc)
        finally:
            logger.info("MES 已離線：%s", peer)
            self.selected = False
            self._writer = None
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _write(self, frame: bytes) -> None:
        if self._writer is None:
            raise ConnectionError("目前沒有連線中的 MES")
        self._writer.write(frame)
        await self._writer.drain()

    async def _on_frame(self, message: dict) -> None:
        stype = message["stype"]
        if stype == secs.ST_SELECT_REQ:
            await self._write(
                secs.encode_message(
                    self.session_id, 0, 0, False, message["system_bytes"], None, secs.ST_SELECT_RSP
                )
            )
            self.selected = True
            logger.info("已接受 Select，進入 SELECTED")
            return
        if stype == secs.ST_LINKTEST_REQ:
            await self._write(
                secs.encode_message(
                    self.session_id, 0, 0, False, message["system_bytes"], None, secs.ST_LINKTEST_RSP
                )
            )
            return
        if stype == secs.ST_SEPARATE_REQ:
            self.selected = False
            return
        if stype != secs.ST_DATA:
            return

        self.received.append(message)
        await self._on_data(message)

    async def _on_data(self, message: dict) -> None:
        stream, function = message["stream"], message["function"]
        reply: tuple[int, dict | None] | None = None

        if (stream, function) == (1, 1):  # Are You There
            reply = (2, secs.L(secs.A(self.model), secs.A(self.softrev)))
        elif (stream, function) == (1, 13):  # Establish Communications
            reply = (14, secs.L(secs.B(0), secs.L(secs.A(self.model), secs.A(self.softrev))))
        elif (stream, function) == (1, 17):  # Request ONLINE
            reply = (18, secs.B(0))
        elif (stream, function) == (2, 31):  # Date and Time Set
            reply = (32, secs.B(0))
        elif (stream, function) == (2, 41):  # Host Command
            command = secs.to_python(message.get("body"))
            name = command[0] if isinstance(command, list) and command else ""
            if str(name).upper() == "START":
                self.state = "EXECUTING"
                await self.send_event(CEID_PROCESS_START, [["EXECUTING"]])
            elif str(name).upper() == "STOP":
                self.state = "IDLE"
                await self.send_event(CEID_PROCESS_END, [["IDLE"]])
            reply = (42, secs.L(secs.B(0), secs.L()))
        elif message.get("w_bit"):
            reply = (0, None)  # 未實作 → SxF0

        if reply is not None:
            await self._write(
                secs.encode_message(
                    self.session_id, stream, reply[0], False, message["system_bytes"], reply[1]
                )
            )

    # ── 主動送出 ────────────────────────────────────────────
    async def send_event(self, ceid: int, reports: list[list[Any]] | None = None) -> None:
        """送出 S6F11 事件報告。"""
        body = secs.L(
            secs.U4(0),
            secs.U4(ceid),
            secs.L(
                *[
                    secs.L(secs.U4(index + 1), secs.L(*[secs.A(str(v)) for v in values]))
                    for index, values in enumerate(reports or [])
                ]
            ),
        )
        await self._write(
            secs.encode_message(self.session_id, 6, 11, True, self._next_system_bytes(), body)
        )

    async def send_alarm(self, alid: int, text: str, raised: bool = True) -> None:
        """送出 S5F1 警報；ALCD 最高位元代表警報發生。"""
        body = secs.L(secs.B(0x80 | 1 if raised else 0x01), secs.U4(alid), secs.A(text))
        await self._write(
            secs.encode_message(self.session_id, 5, 1, True, self._next_system_bytes(), body)
        )

    async def _auto_loop(self) -> None:
        """定期切換狀態並送事件，讓看板上有東西可看。"""
        states = ["EXECUTING", "IDLE", "EXECUTING", "SETUP"]
        index = 0
        while True:
            await asyncio.sleep(self.auto_event_sec)
            if not self.selected or self._writer is None:
                continue
            self.state = states[index % len(states)]
            index += 1
            try:
                await self.send_event(CEID_STATE_CHANGE, [[self.state]])
                if random.random() < 0.15:
                    await self.send_alarm(101, "吸嘴真空不足", raised=True)
            except Exception as exc:
                logger.warning("送出事件失敗：%s", exc)


async def _main() -> None:
    parser = argparse.ArgumentParser(description="SECS/GEM 設備模擬器")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--session-id", type=int, default=0)
    parser.add_argument("--eq-id", default="SIM-01", help="僅用於畫面顯示")
    parser.add_argument("--auto-event-sec", type=float, default=20.0, help="0 表示不自動送事件")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    sim = EquipmentSimulator(
        host=args.host, port=args.port, session_id=args.session_id,
        model=f"SIM-{args.eq_id}", auto_event_sec=args.auto_event_sec,
    )
    await sim.start()
    print(f"設備 {args.eq_id} 模擬器已啟動：{args.host}:{sim.port}（Ctrl+C 結束）")
    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await sim.stop()


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
