import os
import time
import base64
import threading
from dataclasses import dataclass, field
from typing import Optional, Literal

import pexpect


DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_BACKOFF = 2.0
DEFAULT_BUFFER_SIZE = 65536


@dataclass
class ConnectionParams:
    host: str
    port: int
    username: str = ""
    password: str = ""
    key_file: str = ""
    connect_timeout: int = 30
    max_retries: int = DEFAULT_MAX_RETRIES
    retry_backoff: float = DEFAULT_RETRY_BACKOFF


@dataclass
class SSHSession:
    session_id: str
    params: ConnectionParams
    platform: Literal["linux", "windows", "unknown"] = "unknown"
    child: Optional[pexpect.spawn] = None
    connected: bool = False
    reconnect_count: int = 0
    last_error: str = ""
    created_at: float = field(default_factory=time.time)

    def close(self):
        if self.child:
            try:
                self.child.sendline("exit")
            except Exception:
                pass
            try:
                self.child.close()
            except Exception:
                pass
            self.child = None
        self.connected = False


@dataclass
class TelnetSession:
    session_id: str
    params: ConnectionParams
    child: Optional[pexpect.spawn] = None
    connected: bool = False
    reconnect_count: int = 0
    last_error: str = ""
    buffer: bytes = b""
    buffer_max_size: int = DEFAULT_BUFFER_SIZE
    read_cursor: int = 0
    created_at: float = field(default_factory=time.time)

    def close(self):
        if self.child and self.child.isalive():
            try:
                self.child.sendline("exit")
                self.child.close()
            except Exception:
                pass
            self.child = None
        self.connected = False


class SessionManager:
    def __init__(self):
        self._ssh_sessions: dict[str, SSHSession] = {}
        self._telnet_sessions: dict[str, TelnetSession] = {}
        self._lock = threading.Lock()

    # ================================================================
    # SSH: 底层连接 (raw pexpect.spawn，不依赖 pxssh)
    # ================================================================

    def _ssh_spawn(self, params: ConnectionParams) -> pexpect.spawn:
        """构建 SSH 命令，encoding=None 获取原始字节避免编码转换损失。"""
        ssh_args = [
            "ssh",
            "-T",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "PreferredAuthentications=password",
            "-p", str(params.port),
        ]
        if params.key_file:
            ssh_args += ["-i", params.key_file]
        ssh_args.append(f"{params.username}@{params.host}")

        child = pexpect.spawn(ssh_args[0], ssh_args[1:],
                              timeout=params.connect_timeout)

        idx = child.expect(
            [b"password:", b"Password:",
             b"Are you sure you want to continue connecting",
             pexpect.TIMEOUT, pexpect.EOF],
            timeout=30,
        )
        if idx in [0, 1]:
            child.sendline(params.password)
        elif idx == 2:
            child.sendline("yes")
            idx = child.expect(
                [b"password:", b"Password:", pexpect.TIMEOUT, pexpect.EOF],
                timeout=10,
            )
            if idx in [0, 1]:
                child.sendline(params.password)
            elif idx == 2:
                raise ConnectionError("SSH auth timeout after host key confirmation")
            elif idx == 3:
                raise ConnectionError("SSH connection closed after host key confirmation")
        elif idx == 3:
            child.close()
            raise ConnectionError(f"SSH connection timeout to {params.host}:{params.port}")
        elif idx == 4:
            child.close()
            raise ConnectionError(f"SSH connection refused/closed by {params.host}:{params.port}")

        return child

    def _detect_and_setup_prompt(self, child: pexpect.spawn) -> str:
        """
        连接成功后检测远程平台，Windows 则设置工作目录。
        encoding=None，所有 I/O 操作用原始字节。
        返回平台类型: "linux" | "windows"
        """
        platform = "unknown"

        time.sleep(0.3)
        child.sendline("echo __MCP_PLATFORM_DETECT__ && uname -s 2>/dev/null || echo __WINDOWS__ && echo __MCP_DETECT_DONE__")
        try:
            child.expect("__MCP_DETECT_DONE__", timeout=8)
            output = child.before
            if output is None:
                output = b""
            elif isinstance(output, str):
                output = output.encode("utf-8", errors="replace")
            if b"__WINDOWS__" in output:
                platform = "windows"
            else:
                platform = "linux"
        except pexpect.TIMEOUT:
            child.sendline("ver 2>nul && echo __MCP_DETECT_DONE__")
            try:
                child.expect("__MCP_DETECT_DONE__", timeout=5)
                platform = "windows"
            except pexpect.TIMEOUT:
                pass

        time.sleep(0.3)
        try:
            child.read_nonblocking(99999, timeout=0.5)
        except Exception:
            pass

        if platform == "windows":
            self._setup_windows_workspace(child)

        return platform

    def _setup_windows_workspace(self, child: pexpect.spawn):
        """
        切换到 PowerShell 并创建 D:\\remote_debug 工作目录。
        """
        child.sendline("powershell")
        time.sleep(1.5)
        try:
            child.read_nonblocking(99999, timeout=0.5)
        except Exception:
            pass

        child.sendline("New-Item -ItemType Directory -Force -Path D:\\remote_debug | Out-Null; Set-Location D:\\remote_debug; echo __WORKSPACE_READY__")
        try:
            child.expect("__WORKSPACE_READY__", timeout=10)
        except pexpect.TIMEOUT:
            pass

        time.sleep(0.3)
        try:
            child.read_nonblocking(99999, timeout=0.3)
        except Exception:
            pass

    def _do_ssh_connect(self, session: SSHSession) -> str:
        try:
            child = self._ssh_spawn(session.params)
            session.child = child
            session.platform = self._detect_and_setup_prompt(child)
            session.connected = True
            session.reconnect_count = 0
            session.last_error = ""
            return (f"SSH connected: {session.params.username}@"
                    f"{session.params.host}:{session.params.port}"
                    f" [{session.platform}] [session={session.session_id}]")
        except Exception as e:
            session.last_error = str(e)
            session.close()
            raise

    def ssh_connect(self, session_id: str, host: str, port: int,
                    username: str, password: str,
                    max_retries: int = DEFAULT_MAX_RETRIES) -> str:
        params = ConnectionParams(
            host=host, port=port, username=username, password=password,
            max_retries=max_retries,
        )
        with self._lock:
            if session_id in self._ssh_sessions:
                self._ssh_sessions[session_id].close()
            session = SSHSession(session_id=session_id, params=params)
            self._ssh_sessions[session_id] = session
        try:
            return self._do_ssh_connect(session)
        except Exception as e:
            return f"SSH connection failed [{session_id}]: {e}"

    def ssh_connect_key(self, session_id: str, host: str, port: int,
                         username: str, key_file: str,
                         max_retries: int = DEFAULT_MAX_RETRIES) -> str:
        params = ConnectionParams(
            host=host, port=port, username=username, key_file=key_file,
            max_retries=max_retries,
        )
        with self._lock:
            if session_id in self._ssh_sessions:
                self._ssh_sessions[session_id].close()
            session = SSHSession(session_id=session_id, params=params)
            self._ssh_sessions[session_id] = session
        try:
            return self._do_ssh_connect(session)
        except Exception as e:
            return f"SSH key connection failed [{session_id}]: {e}"

    # ================================================================
    # SSH: 命令执行
    # ================================================================

    def _ssh_execute_inner(self, session: SSHSession, command: str,
                           timeout: int) -> str:
        child = session.child
        marker = f"__MCP_CMD_{int(time.time() * 1000)}__"
        marker_bytes = marker.encode("utf-8")

        full_cmd = f"{command}; echo {marker}"
        if session.platform == "windows":
            full_cmd_bytes = full_cmd.encode("gbk", errors="replace")
        else:
            full_cmd_bytes = full_cmd.encode("utf-8")

        try:
            child.read_nonblocking(99999, timeout=0.3)
        except Exception:
            pass

        child.send(full_cmd_bytes + b"\n")

        deadline = time.time() + timeout
        all_data = b""
        marker_found = False

        while time.time() < deadline:
            time.sleep(0.3)
            try:
                chunk = child.read_nonblocking(99999, timeout=0.3)
                if chunk:
                    all_data += chunk
                    if marker_bytes in all_data:
                        marker_found = True
                        break
            except pexpect.TIMEOUT:
                continue
            except pexpect.EOF:
                break

        if not marker_found:
            return f"[TIMEOUT] Command exceeded {timeout}s: {command}"

        parts = all_data.rsplit(marker_bytes, 1)
        raw = parts[0] if len(parts) > 0 else b""

        if session.platform == "windows":
            try:
                output = raw.decode("gbk")
            except (UnicodeDecodeError, LookupError):
                output = raw.decode("utf-8", errors="replace")
        else:
            output = raw.decode("utf-8", errors="replace")

        output = output.replace("\r\n", "\n").replace("\r", "\n").strip()
        cmd_prefix = command.strip()
        lines = output.split("\n")
        cleaned = []
        for line in lines:
            s = line.strip()
            if not s:
                continue
            if marker in s:
                continue
            if cmd_prefix and s.startswith(cmd_prefix):
                continue
            cleaned.append(s)
        return "\n".join(cleaned).strip()

    def ssh_execute(self, session_id: str, command: str,
                    timeout: int = 30) -> str:
        session = self._ssh_sessions.get(session_id)
        if not session:
            return f"SSH session not found: {session_id}"
        if not session.connected:
            return f"SSH session not connected: {session_id}"

        try:
            return self._ssh_execute_inner(session, command, timeout)
        except (pexpect.EOF, OSError) as e:
            return self._try_reconnect_ssh(session, command, timeout, str(e))
        except Exception as e:
            session.connected = False
            session.last_error = str(e)
            return f"SSH execute error [{session_id}]: {e}"

    def _try_reconnect_ssh(self, session: SSHSession, command: str,
                           timeout: int, error_msg: str) -> str:
        if session.reconnect_count >= session.params.max_retries:
            session.connected = False
            session.last_error = error_msg
            return (f"SSH connection lost [{session.session_id}]: "
                    f"{error_msg} (max retries exceeded)")

        session.reconnect_count += 1
        backoff = session.params.retry_backoff * (2 ** (session.reconnect_count - 1))
        time.sleep(backoff)

        try:
            session.connected = False
            try:
                session.child.close()
            except Exception:
                pass
            session.child = None

            self._do_ssh_connect(session)
            result = self._ssh_execute_inner(session, command, timeout)
            return f"[Reconnected after {session.reconnect_count} retries]\n{result}"
        except Exception as e:
            return self._try_reconnect_ssh(session, command, timeout, str(e))

    # ================================================================
    # SSH: 断开与会话列表
    # ================================================================

    def ssh_disconnect(self, session_id: str) -> str:
        with self._lock:
            session = self._ssh_sessions.pop(session_id, None)
        if session:
            session.close()
            return f"SSH disconnected: {session_id}"
        return f"SSH session not found: {session_id}"

    def ssh_list(self) -> str:
        lines = []
        for sid, s in self._ssh_sessions.items():
            status = "connected" if s.connected else "disconnected"
            plat = f" {s.platform}" if s.platform != "unknown" else ""
            lines.append(
                f"  [{sid}] {s.params.username}@{s.params.host}:"
                f"{s.params.port}{plat} ({status})"
            )
        return "\n".join(lines) if lines else "No SSH sessions."

    # ================================================================
    # SSH: 文件传输 (SCP 优先 → SFTP 兜底)
    # ================================================================

    def _normalize_remote_path(self, session: SSHSession, remote_path: str) -> str:
        """根据远程平台规范化路径格式。
        Windows 路径转为 /D:/path 格式供 SCP/SFTP 使用。"""
        if session.platform == "windows":
            remote_path = remote_path.replace("\\", "/")
            if not remote_path.startswith("/"):
                remote_path = "/" + remote_path
        return remote_path

    def ssh_upload(self, session_id: str, local_path: str,
                   remote_path: str) -> str:
        session = self._ssh_sessions.get(session_id)
        if not session:
            return f"SSH session not found: {session_id}"
        if not os.path.exists(local_path):
            return f"Local file not found: {local_path}"

        remote_path = self._normalize_remote_path(session, remote_path)

        result = self._scp_transfer(
            session, local_path,
            f"{session.params.username}@{session.params.host}:{remote_path}",
        )
        if "OK" in result:
            return result

        result2 = self._sftp_transfer(session, local_path, remote_path, put=True)
        if "OK" in result2:
            return result2

        return f"SSH upload failed [{session_id}]: SCP({result}) / SFTP({result2})"

    def ssh_download(self, session_id: str, remote_path: str,
                     local_path: str) -> str:
        session = self._ssh_sessions.get(session_id)
        if not session:
            return f"SSH session not found: {session_id}"

        remote_path = self._normalize_remote_path(session, remote_path)

        result = self._scp_transfer(
            session,
            f"{session.params.username}@{session.params.host}:{remote_path}",
            local_path,
        )
        if "OK" in result:
            return result

        result2 = self._sftp_transfer(session, local_path, remote_path, put=False)
        if "OK" in result2:
            return result2

        return f"SSH download failed [{session_id}]: SCP({result}) / SFTP({result2})"

    def _scp_transfer(self, session: SSHSession, src: str,
                      dst: str) -> str:
        """SCP 传输，使用 pexpect 直连（兼容密码认证）。"""
        password = session.params.password
        port = session.params.port
        args = [
            "scp", "-P", str(port),
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            src, dst,
        ]
        try:
            child = pexpect.spawn(args[0], args[1:],
                                  timeout=60, encoding="utf-8",
                                  codec_errors="replace")
            i = child.expect(
                ["password:", "Password:",
                 "Are you sure you want to continue connecting",
                 pexpect.EOF, pexpect.TIMEOUT],
                timeout=15,
            )
            if i in [0, 1] and password:
                child.sendline(password)
                child.expect(pexpect.EOF, timeout=60)
            elif i == 2:
                child.sendline("yes")
                idx = child.expect(["password:", "Password:", pexpect.EOF], timeout=15)
                if idx in [0, 1] and password:
                    child.sendline(password)
                child.expect(pexpect.EOF, timeout=60)

            child.close()
            if child.exitstatus == 0:
                return f"SCP transfer OK [{session.session_id}]: {src} -> {dst}"
            output = (child.before or "")[:500]
            return f"SCP failed (exit={child.exitstatus}) [{session.session_id}]: {output}"
        except Exception as e:
            return f"SCP error [{session.session_id}]: {e}"
        except Exception as e:
            return f"SCP error [{session.session_id}]: {e}"

    def _sftp_transfer(self, session: SSHSession, local_path: str,
                       remote_path: str, put: bool = True) -> str:
        """SFTP 传输。上传前自动创建父目录（UTF-8 编码，兼容中文）。"""
        password = session.params.password
        port = session.params.port
        host = session.params.host
        user = session.params.username
        args = [
            "sftp", "-P", str(port),
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "BatchMode=no",
            f"{user}@{host}",
        ]
        try:
            child = pexpect.spawn(args[0], args[1:],
                                  timeout=60, encoding="utf-8",
                                  codec_errors="replace")
            idx = child.expect(
                ["password:", "Password:", "sftp>", pexpect.TIMEOUT, pexpect.EOF],
                timeout=15,
            )
            if idx in [0, 1] and password:
                child.sendline(password)
                child.expect("sftp>", timeout=10)
            elif idx != 2:
                child.close()
                return f"SFTP auth failed [{session.session_id}]"

            if put:
                self._sftp_mkdirs(child, remote_path)
                child.sendline(f'put "{local_path}" "{remote_path}"')
            else:
                child.sendline(f'get "{remote_path}" "{local_path}"')

            idx = child.expect(["sftp>", pexpect.TIMEOUT], timeout=30)
            output = child.before or ""
            child.sendline("bye")
            child.expect(pexpect.EOF, timeout=5)
            child.close()

            if "not found" in output.lower() or "no such file" in output.lower():
                return f"SFTP not found [{session.session_id}]: {remote_path}"
            if "error" in output.lower() or "couldn't" in output.lower():
                return f"SFTP error [{session.session_id}]: {output[-200:]}"
            if not put and not os.path.exists(local_path):
                return f"SFTP no local file [{session.session_id}]: {local_path}"
            return f"SFTP transfer OK [{session.session_id}]: {local_path} -> {remote_path}"
        except Exception as e:
            return f"SFTP error [{session.session_id}]: {e}"

    @staticmethod
    def _sftp_mkdirs(child, remote_path: str):
        """通过 SFTP mkdir 逐级创建远程父目录（UTF-8 编码，兼容中文）。"""
        parent = remote_path.rsplit("/", 1)[0]
        if not parent or parent == remote_path:
            return
        parts = parent.lstrip("/").split("/")
        current = ""
        for part in parts:
            if not part:
                continue
            if current:
                current += "/" + part
            else:
                current = "/" + part
            child.sendline(f'mkdir "{current}"')
            child.expect(["sftp>", pexpect.TIMEOUT], timeout=5)

    # ================================================================
    # Telnet: 连接
    # ================================================================

    def _do_telnet_connect(self, session: TelnetSession) -> str:
        try:
            child = pexpect.spawn(
                "telnet",
                [session.params.host, str(session.params.port)],
                timeout=session.params.connect_timeout,
                encoding="utf-8",
            )
            idx = child.expect(
                ["Escape character is", "login:", "Login:", "Username:",
                 "User:", "username:", pexpect.TIMEOUT, pexpect.EOF],
                timeout=session.params.connect_timeout,
            )
            if idx in [6, 7]:
                child.close()
                raise ConnectionError("Telnet connection timeout or EOF")

            if idx in [1, 2, 3, 4, 5] and session.params.username:
                child.sendline(session.params.username)
                child.expect(["Password:", "password:"], timeout=10)
                child.sendline(session.params.password)
                child.expect(["$", "#", ">", ":", pexpect.TIMEOUT], timeout=10)

            session.child = child
            session.connected = True
            session.reconnect_count = 0
            session.last_error = ""
            session.buffer = b""
            session.read_cursor = 0
            return (f"Telnet connected: {session.params.host}:"
                    f"{session.params.port} [session={session.session_id}]")
        except Exception as e:
            session.last_error = str(e)
            session.close()
            raise

    def telnet_connect(self, session_id: str, host: str, port: int,
                        username: str = "", password: str = "",
                        timeout: int = 15,
                        buffer_max_size: int = DEFAULT_BUFFER_SIZE,
                        max_retries: int = DEFAULT_MAX_RETRIES) -> str:
        params = ConnectionParams(
            host=host, port=port, username=username, password=password,
            connect_timeout=timeout, max_retries=max_retries,
        )
        with self._lock:
            if session_id in self._telnet_sessions:
                self._telnet_sessions[session_id].close()
            session = TelnetSession(
                session_id=session_id, params=params,
                buffer_max_size=buffer_max_size,
            )
            self._telnet_sessions[session_id] = session

        try:
            return self._do_telnet_connect(session)
        except Exception as e:
            return f"Telnet connection failed [{session_id}]: {e}"

    # ================================================================
    # Telnet: 数据收发
    # ================================================================

    def _telnet_expect_data(self, child: pexpect.spawn,
                            timeout: float) -> bytes:
        try:
            idx = child.expect([r".+", pexpect.TIMEOUT, pexpect.EOF],
                               timeout=min(timeout, 1.0))
            if idx == 0:
                out = child.after
                if isinstance(out, str):
                    out = out.encode("utf-8", errors="replace")
                return out
        except Exception:
            pass
        return b""

    def _append_to_buffer(self, session: TelnetSession, data: bytes):
        session.buffer += data
        if len(session.buffer) > session.buffer_max_size:
            overflow = len(session.buffer) - session.buffer_max_size
            session.buffer = session.buffer[overflow:]
            session.read_cursor = max(0, session.read_cursor - overflow)

    def _read_new_data(self, session: TelnetSession,
                       encoding: str = "utf-8") -> str:
        if session.read_cursor >= len(session.buffer):
            return ""
        raw = session.buffer[session.read_cursor:]
        session.read_cursor = len(session.buffer)
        return self._encode_bytes(raw, encoding)

    def _read_all_data(self, session: TelnetSession,
                       encoding: str = "utf-8") -> str:
        raw = session.buffer
        session.read_cursor = len(session.buffer)
        return self._encode_bytes(raw, encoding)

    @staticmethod
    def _encode_bytes(data: bytes, encoding: str) -> str:
        if encoding == "base64":
            return base64.b64encode(data).decode()
        elif encoding == "hex":
            return data.hex()
        else:
            return data.decode(encoding, errors="replace")

    def telnet_execute(self, session_id: str, command: str,
                       timeout: int = 5) -> str:
        session = self._telnet_sessions.get(session_id)
        if not session or not session.connected:
            return f"Telnet session not found or not connected: {session_id}"

        try:
            child = session.child
            child.sendline(command)
            child.expect([pexpect.TIMEOUT, pexpect.EOF], timeout=timeout)
            output = child.before
            if output is None:
                return ""
            if isinstance(output, bytes):
                output = output.decode("utf-8", errors="replace")
            return output.replace("\r\n", "\n").strip()
        except (pexpect.EOF, OSError) as e:
            return self._try_reconnect_telnet(session, str(e))
        except Exception as e:
            session.connected = False
            session.last_error = str(e)
            return f"Telnet execute error [{session_id}]: {e}"

    def telnet_send(self, session_id: str, data: str) -> str:
        session = self._telnet_sessions.get(session_id)
        if not session or not session.connected:
            return f"Telnet session not found or not connected: {session_id}"
        try:
            session.child.send(data)
            return f"Data sent to [{session_id}]"
        except (pexpect.EOF, OSError) as e:
            self._try_reconnect_telnet(session, str(e), silent=True)
            return f"Data sent to [{session_id}] (reconnected)"
        except Exception as e:
            session.connected = False
            session.last_error = str(e)
            return f"Telnet send error [{session_id}]: {e}"

    def telnet_listen(self, session_id: str, duration: int = 10,
                      encoding: str = "utf-8") -> str:
        session = self._telnet_sessions.get(session_id)
        if not session or not session.connected:
            return f"Telnet session not found or not connected: {session_id}"

        try:
            child = session.child
            end_time = time.time() + duration
            while time.time() < end_time:
                remaining = max(0.1, end_time - time.time())
                data = self._telnet_expect_data(child, remaining)
                if data:
                    self._append_to_buffer(session, data)
                else:
                    time.sleep(0.05)

            result = self._read_new_data(session, encoding)
            return result if result else "(no data received)"
        except (pexpect.EOF, OSError) as e:
            partial = self._read_new_data(session, encoding)
            if partial:
                return f"{partial}\n[Connection lost: {e}]"
            return f"Telnet connection lost [{session_id}]: {e}"
        except Exception as e:
            return f"Telnet listen error [{session_id}]: {e}"

    def telnet_read(self, session_id: str, timeout: int = 3,
                    encoding: str = "utf-8") -> str:
        session = self._telnet_sessions.get(session_id)
        if not session or not session.connected:
            return f"Telnet session not found or not connected: {session_id}"

        try:
            child = session.child
            end_time = time.time() + timeout
            while time.time() < end_time:
                remaining = max(0.1, end_time - time.time())
                data = self._telnet_expect_data(child, remaining)
                if data:
                    self._append_to_buffer(session, data)
                else:
                    break

            result = self._read_new_data(session, encoding)
            return result if result else "(no new data)"
        except (pexpect.EOF, OSError) as e:
            partial = self._read_new_data(session, encoding)
            return partial if partial else f"Telnet connection lost [{session_id}]: {e}"
        except Exception as e:
            return f"Telnet read error [{session_id}]: {e}"

    def telnet_read_all(self, session_id: str,
                        encoding: str = "utf-8") -> str:
        session = self._telnet_sessions.get(session_id)
        if not session or not session.connected:
            return f"Telnet session not found or not connected: {session_id}"

        try:
            child = session.child
            while True:
                data = self._telnet_expect_data(child, 0.5)
                if data:
                    self._append_to_buffer(session, data)
                else:
                    break

            result = self._read_all_data(session, encoding)
            return result if result else "(no data in buffer)"
        except Exception as e:
            return f"Telnet read_all error [{session_id}]: {e}"

    # ================================================================
    # Telnet: 重连 / 断开 / 列表
    # ================================================================

    def _try_reconnect_telnet(self, session: TelnetSession,
                              error_msg: str = "",
                              silent: bool = False) -> str:
        if session.reconnect_count >= session.params.max_retries:
            session.connected = False
            session.last_error = error_msg
            return (f"Telnet connection lost [{session.session_id}]: "
                    f"{error_msg} (max retries exceeded)")

        session.reconnect_count += 1
        backoff = session.params.retry_backoff * (2 ** (session.reconnect_count - 1))
        time.sleep(backoff)

        try:
            session.connected = False
            try:
                session.child.close()
            except Exception:
                pass
            session.child = None

            self._do_telnet_connect(session)
            return (f"Telnet reconnected [{session.session_id}] "
                    f"after {session.reconnect_count} retries")
        except Exception as e:
            if not silent:
                return self._try_reconnect_telnet(session, str(e))
            session.connected = False
            return f"Telnet reconnect failed [{session.session_id}]: {e}"

    def telnet_disconnect(self, session_id: str) -> str:
        with self._lock:
            session = self._telnet_sessions.pop(session_id, None)
        if session:
            session.close()
            return f"Telnet disconnected: {session_id}"
        return f"Telnet session not found: {session_id}"

    def telnet_list(self) -> str:
        lines = []
        for sid, s in self._telnet_sessions.items():
            status = "connected" if s.connected else "disconnected"
            buf_kb = len(s.buffer) / 1024
            lines.append(
                f"  [{sid}] {s.params.host}:{s.params.port} ({status}) "
                f"buffer={buf_kb:.1f}KB"
            )
        return "\n".join(lines) if lines else "No Telnet sessions."

    # ================================================================
    # 汇总
    # ================================================================

    def list_all(self) -> str:
        ssh = self.ssh_list()
        telnet = self.telnet_list()
        return f"SSH Sessions:\n{ssh}\n\nTelnet Sessions:\n{telnet}"


_manager: Optional[SessionManager] = None


def get_manager() -> SessionManager:
    global _manager
    if _manager is None:
        _manager = SessionManager()
    return _manager
