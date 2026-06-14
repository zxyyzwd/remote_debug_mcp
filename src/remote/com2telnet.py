#!/usr/bin/env python3
"""
com2telnet - Serial Port to Telnet Server
=========================================
Exposes serial (COM) ports as TCP/telnet servers. Supports multiple serial ports
and multiple concurrent telnet clients on Windows.

Requirements: Python 3.8+, pyserial

Usage:
  python com2telnet.py --serial COM3:5023
  python com2telnet.py --serial COM3:5023:115200 --serial COM4:5024:9600
  python com2telnet.py --config config.json
  python com2telnet.py --list

Config file (JSON):
  {
    "host": "0.0.0.0",
    "mappings": [
      {"port": "COM3", "telnet_port": 5023, "baudrate": 115200},
      {"port": "COM4", "telnet_port": 5024, "baudrate": 9600}
    ]
  }
"""

import asyncio
import argparse
import json
import logging
import sys
import signal
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional, Set, Tuple

import serial
import serial.tools.list_ports

# ── Telnet protocol constants (RFC 854) ────────────────────────────────────────
IAC = 0xFF  # Interpret As Command
DONT = 0xFE
DO = 0xFD
WONT = 0xFC
WILL = 0xFB
SB = 0xFA  # Subnegotiation Begin
SE = 0xF0  # Subnegotiation End

TELOPT_ECHO = 1
TELOPT_SGA = 3  # Suppress Go Ahead (character-at-a-time mode)

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("com2telnet")


# ═══════════════════════════════════════════════════════════════════════════════
# SerialPortManager
# ═══════════════════════════════════════════════════════════════════════════════

class SerialPortManager:
    """Manages a single serial port and broadcasts data to connected clients."""

    def __init__(
        self,
        port: str,
        baudrate: int = 115200,
        bytesize: int = 8,
        parity: str = "N",
        stopbits: int = 1,
        timeout: float = 0.0,
    ):
        self.port = port
        self.baudrate = baudrate
        self.bytesize = bytesize
        self.parity = parity
        self.stopbits = stopbits
        self.timeout = timeout

        self._serial: Optional[serial.Serial] = None
        self._clients: Set[asyncio.StreamWriter] = set()
        self._clients_lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._shutdown = threading.Event()
        self._reader_thread: Optional[threading.Thread] = None
        self._write_executor = ThreadPoolExecutor(max_workers=1)

        self.name = f"serial:{port}"

    # ── start / stop ─────────────────────────────────────────────────────────

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self._serial = serial.Serial(
            port=self.port,
            baudrate=self.baudrate,
            bytesize=self.bytesize,
            parity=self.parity,
            stopbits=self.stopbits,
            timeout=self.timeout,
            write_timeout=0,
        )
        self._loop = loop
        self._shutdown.clear()
        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()
        logger.info("%s opened (%d baud)", self.name, self.baudrate)

    async def stop(self) -> None:
        self._shutdown.set()
        if self._reader_thread is not None:
            self._reader_thread.join(timeout=3)

        with self._clients_lock:
            clients = list(self._clients)
            self._clients.clear()
        for writer in clients:
            try:
                writer.close()
            except Exception:
                pass

        if self._serial is not None and self._serial.is_open:
            self._serial.close()
            logger.info("%s closed", self.name)

        self._write_executor.shutdown(wait=True)

    # ── serial reader (background thread) ────────────────────────────────────

    def _reader_loop(self) -> None:
        while not self._shutdown.is_set():
            try:
                if self._serial is None or not self._serial.is_open:
                    self._shutdown.wait(0.1)
                    continue
                n = self._serial.in_waiting
                if n > 0:
                    data = self._serial.read(n)
                    if data:
                        asyncio.run_coroutine_threadsafe(
                            self._broadcast(data), self._loop  # type: ignore[arg-type]
                        )
                else:
                    self._shutdown.wait(0.01)
            except serial.SerialException as e:
                logger.error("%s read error: %s", self.name, e)
                asyncio.run_coroutine_threadsafe(
                    self._notify_error(str(e)), self._loop  # type: ignore[arg-type]
                )
                break
            except OSError as e:
                logger.error("%s OS error: %s", self.name, e)
                break
            except Exception as e:
                logger.error("%s unexpected error: %s", self.name, e)
                self._shutdown.wait(0.5)

    async def _notify_error(self, msg: str) -> None:
        logger.warning("%s reader thread exited: %s", self.name, msg)

    async def _broadcast(self, data: bytes) -> None:
        with self._clients_lock:
            writers = list(self._clients)
        if not writers:
            return

        for writer in writers:
            try:
                writer.write(data)
            except Exception:
                pass

        async def _drain_one(writer):
            try:
                await writer.drain()
            except Exception:
                await self.remove_client(writer)

        await asyncio.gather(*[_drain_one(w) for w in writers],
                             return_exceptions=True)

    # ── client management ────────────────────────────────────────────────────

    async def add_client(self, writer: asyncio.StreamWriter) -> None:
        with self._clients_lock:
            self._clients.add(writer)
        addr = writer.get_extra_info("peername")
        logger.info("%s client %s connected (%d total)", self.name, addr, len(self._clients))

    async def remove_client(self, writer: asyncio.StreamWriter) -> None:
        with self._clients_lock:
            if writer not in self._clients:
                return
            self._clients.discard(writer)
        addr = writer.get_extra_info("peername")
        logger.info("%s client %s disconnected (%d total)", self.name, addr, len(self._clients))
        try:
            writer.close()
        except Exception:
            pass

    # ── write to serial (async-safe) ─────────────────────────────────────────

    async def write(self, data: bytes) -> None:
        if self._serial is not None and self._serial.is_open:
            loop = asyncio.get_running_loop()
            try:
                await loop.run_in_executor(self._write_executor, self._serial.write, data)
            except serial.SerialException:
                pass


# ═══════════════════════════════════════════════════════════════════════════════
# TelnetProtocol — minimal RFC 854 negotiation handler
# ═══════════════════════════════════════════════════════════════════════════════

class TelnetProtocol:
    """Filters telnet commands from a byte stream and returns negotiation responses."""

    def __init__(self) -> None:
        self._buf = b""
        self._sent_will_sga = False
        self._sent_will_echo = False

    def mark_sga_sent(self) -> None:
        self._sent_will_sga = True

    def mark_will_echo_sent(self) -> None:
        self._sent_will_echo = True

    def feed(self, data: bytes) -> Tuple[bytes, bytes]:
        """
        Process incoming data.
        Returns (payload_data, telnet_responses_to_send).
        """
        buf = self._buf + data
        self._buf = b""
        payload = bytearray()
        responses = bytearray()
        i = 0

        while i < len(buf):
            b = buf[i]
            if b == IAC:
                if i + 1 >= len(buf):
                    self._buf = buf[i:]
                    break
                cmd = buf[i + 1]

                if cmd == IAC:
                    payload.append(IAC)  # escaped 0xFF data byte
                    i += 2
                elif cmd in (DO, DONT, WILL, WONT):
                    if i + 2 >= len(buf):
                        self._buf = buf[i:]
                        break
                    opt = buf[i + 2]
                    resp = self._negotiate(cmd, opt)
                    if resp:
                        responses.extend(resp)
                    i += 3
                elif cmd == SB:
                    j = i + 2
                    while j + 1 < len(buf):
                        if buf[j] == IAC and buf[j + 1] == SE:
                            break
                        j += 1
                    if j + 1 >= len(buf):
                        self._buf = buf[i:]
                        break
                    i = j + 2
                else:
                    i += 2
            else:
                payload.append(b)
                i += 1

        return bytes(payload), bytes(responses)

    def _negotiate(self, cmd: int, opt: int) -> Optional[bytes]:
        if cmd == WILL:
            if opt == TELOPT_SGA:
                return bytes([IAC, DO, opt])
            return bytes([IAC, DONT, opt])
        elif cmd == DO:
            if opt == TELOPT_SGA:
                if not self._sent_will_sga:
                    self._sent_will_sga = True
                    return bytes([IAC, WILL, opt])
                return None
            if opt == TELOPT_ECHO:
                if not self._sent_will_echo:
                    self._sent_will_echo = True
                    return bytes([IAC, WILL, opt])
                return None
            return bytes([IAC, WONT, opt])
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# CRLF translation helper
# ═══════════════════════════════════════════════════════════════════════════════

def translate_crlf(data: bytes) -> bytes:
    """Translate standalone CR / LF / CR+NUL to CR+LF for serial devices.

    Handles three line-ending conventions commonly seen from telnet clients:
      - CR+LF  → CR+LF  (already correct, pass through)
      - CR+NUL → CR+LF  (telnet binary-mode carriage return)
      - CR     → CR+LF  (bare carriage return)
      - LF     → CR+LF  (bare line feed)
    """
    if b"\r" not in data and b"\n" not in data:
        return data
    result = bytearray()
    i = 0
    while i < len(data):
        b = data[i]
        if b == 0x0D:  # CR
            if i + 1 < len(data):
                nxt = data[i + 1]
                if nxt == 0x0A:  # CR+LF
                    result.extend(b"\r\n")
                    i += 2
                    continue
                if nxt == 0x00:  # CR+NUL (telnet binary)
                    result.extend(b"\r\n")
                    i += 2
                    continue
            result.extend(b"\r\n")  # standalone CR
            i += 1
        elif b == 0x0A:  # standalone LF
            result.extend(b"\r\n")
            i += 1
        else:
            result.append(b)
            i += 1
    return bytes(result)


def translate_del_to_bs(data: bytes) -> bytes:
    """Translate DEL (0x7F) to BS (0x08) for serial devices that use BS for backspace.
    Many embedded serial consoles expect BS rather than DEL for backspace key."""
    if 0x7F not in data:
        return data
    return data.replace(b"\x7f", b"\x08")


def _strip_nul(data: bytes) -> bytes:
    """Strip NUL (0x00) bytes commonly sent by telnet binary mode after CR."""
    if 0x00 not in data:
        return data
    return data.replace(b"\x00", b"")


# ═══════════════════════════════════════════════════════════════════════════════
# Client handler
# ═══════════════════════════════════════════════════════════════════════════════

async def handle_telnet_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    manager: SerialPortManager,
    crlf: bool = True,
    del_to_bs: bool = True,
    strip_nul: bool = True,
) -> None:
    addr = writer.get_extra_info("peername")

    telnet = TelnetProtocol()
    await manager.add_client(writer)

    # Advertise character-at-a-time mode & server-side echo (device provides it)
    init = bytes([IAC, WILL, TELOPT_SGA, IAC, WILL, TELOPT_ECHO])
    telnet.mark_sga_sent()
    telnet.mark_will_echo_sent()
    try:
        writer.write(init)
        await writer.drain()
    except Exception:
        pass

    try:
        while True:
            try:
                data = await reader.read(4096)
            except (ConnectionError, OSError):
                break

            if not data:
                break

            payload, responses = telnet.feed(data)
            if responses:
                try:
                    writer.write(responses)
                    await writer.drain()
                except Exception:
                    pass
            if payload:
                if strip_nul:
                    payload = _strip_nul(payload)
                if del_to_bs:
                    payload = translate_del_to_bs(payload)
                if crlf:
                    payload = translate_crlf(payload)
                await manager.write(payload)

    except asyncio.CancelledError:
        pass
    finally:
        await manager.remove_client(writer)
        try:
            writer.close()
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════════════
# Server runner
# ═══════════════════════════════════════════════════════════════════════════════

async def run_server(
    mappings: List[Tuple[str, int, int]], host: str = "0.0.0.0",
    crlf: bool = True, del_to_bs: bool = True, strip_nul: bool = True,
) -> None:
    loop = asyncio.get_running_loop()
    managers: Dict[str, SerialPortManager] = {}
    servers: List[asyncio.Server] = []

    try:
        for port, telnet_port, baudrate in mappings:
            mgr = SerialPortManager(port=port, baudrate=baudrate)
            mgr.start(loop)
            managers[port] = mgr

            server = await asyncio.start_server(
                lambda r, w, m=mgr, crlf=crlf, d2b=del_to_bs, snul=strip_nul:
                    handle_telnet_client(r, w, m, crlf=crlf, del_to_bs=d2b, strip_nul=snul),
                host=host,
                port=telnet_port,
            )
            servers.append(server)
            flags = []
            if crlf: flags.append("crlf")
            if del_to_bs: flags.append("del→bs")
            if strip_nul: flags.append("strip-nul")
            tag = f" [{', '.join(flags)}]" if flags else ""
            logger.info(
                "telnet://%s:%d  -->  %s (%d baud)%s",
                host, telnet_port, port, baudrate, tag,
            )

        logger.info("Ready.  %d serial port(s) mapped.  Press Ctrl+C to stop.", len(mappings))

        # Block until cancelled
        await asyncio.Event().wait()

    except asyncio.CancelledError:
        pass
    finally:
        logger.info("Shutting down...")
        for server in servers:
            server.close()
            await server.wait_closed()
        for mgr in managers.values():
            await mgr.stop()
        logger.info("All servers stopped.")


# ═══════════════════════════════════════════════════════════════════════════════
# CLI helpers
# ═══════════════════════════════════════════════════════════════════════════════

def parse_spec(spec: str) -> Tuple[str, int, int]:
    parts = spec.split(":")
    if len(parts) < 2:
        raise ValueError(
            f"Invalid mapping '{spec}'.  Expected PORT:TELNET_PORT[:BAUDRATE]"
        )
    port = parts[0]
    telnet_port = int(parts[1])
    baudrate = int(parts[2]) if len(parts) > 2 else 115200
    return port, telnet_port, baudrate


def list_ports() -> None:
    ports = serial.tools.list_ports.comports()
    if not ports:
        print("No serial ports detected.")
        return
    print(f"{'Device':<10}  Description")
    print(f"{'------':<10}  -----------")
    for p in sorted(ports, key=lambda x: x.device):
        print(f"{p.device:<10}  {p.description}")


# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(
        description="com2telnet — Serial Port to Telnet Server",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  python com2telnet.py --serial COM3:5023
  python com2telnet.py --serial COM3:5023:115200 --serial COM4:5024:9600
  python com2telnet.py --config config.json
  python com2telnet.py --list""",
    )
    parser.add_argument(
        "--serial", "-s",
        action="append",
        default=[],
        help="Mapping: PORT:TELNET_PORT[:BAUDRATE]  (baudrate default: 115200)",
    )
    parser.add_argument("--config", "-c", help="Path to JSON config file")
    parser.add_argument(
        "--host", default="0.0.0.0", help="Bind address for telnet servers (default: 0.0.0.0)"
    )
    parser.add_argument(
        "--list", "-l", action="store_true", help="List available serial ports and exit"
    )
    parser.add_argument(
        "--debug", "-d", action="store_true", help="Enable debug-level logging"
    )
    parser.add_argument(
        "--no-crlf", action="store_false", dest="crlf",
        help="Disable CR/LF→CR+LF translation (CR/LF/CR+NUL → CR+LF is on by default)",
    )
    parser.add_argument(
        "--no-del-to-bs", action="store_false", dest="del_to_bs",
        help="Disable DEL→BS translation (DEL 0x7F → BS 0x08 is on by default)",
    )
    parser.add_argument(
        "--no-strip-nul", action="store_false", dest="strip_nul",
        help="Disable NUL byte stripping (NUL stripping is on by default)",
    )

    args = parser.parse_args()

    if args.debug:
        logger.setLevel(logging.DEBUG)
        for h in logger.handlers:
            h.setLevel(logging.DEBUG)

    if args.list:
        list_ports()
        return

    # Build mapping list
    mappings: List[Tuple[str, int, int]] = []

    for spec in args.serial:
        mappings.append(parse_spec(spec))

    if args.config:
        with open(args.config, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        host = cfg.get("host", args.host)
        for m in cfg.get("mappings", []):
            mappings.append(
                (m["port"], m["telnet_port"], m.get("baudrate", 115200))
            )
    else:
        host = args.host

    if not mappings:
        parser.print_help()
        print("\nError: provide at least one --serial mapping or a --config file.")
        sys.exit(1)

    # Sanity checks
    used_telnet = set()
    for port, tport, baud in mappings:
        if tport in used_telnet:
            print(f"Error: telnet port {tport} is already mapped.")
            sys.exit(1)
        if tport < 1 or tport > 65535:
            print(f"Error: telnet port {tport} out of range (1-65535).")
            sys.exit(1)
        used_telnet.add(tport)

    # Run
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    main_task = loop.create_task(run_server(mappings, host, crlf=args.crlf, del_to_bs=args.del_to_bs, strip_nul=args.strip_nul))

    async def _do_shutdown() -> None:
        main_task.cancel()

    def _trigger_shutdown() -> None:
        loop.call_soon_threadsafe(lambda: asyncio.ensure_future(_do_shutdown()))

    if sys.platform == "win32":
        signal.signal(signal.SIGINT, lambda s, f: _trigger_shutdown())
    else:
        try:
            loop.add_signal_handler(signal.SIGINT, _trigger_shutdown)
            loop.add_signal_handler(signal.SIGTERM, _trigger_shutdown)
        except NotImplementedError:
            signal.signal(signal.SIGINT, lambda s, f: _trigger_shutdown())

    try:
        loop.run_until_complete(main_task)
    except KeyboardInterrupt:
        pass
    finally:
        loop.close()


if __name__ == "__main__":
    main()
