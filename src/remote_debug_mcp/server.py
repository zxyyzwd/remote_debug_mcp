import os
import asyncio

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from remote_debug_mcp.sessions import get_manager
from remote_debug_mcp.config_loader import (
    load_config, get_config, reload_config, save_config,
)

server = Server("remote-debug-mcp")

_PKG_DIR = os.path.dirname(os.path.abspath(__file__))
REMOTE_TOOLS_DIR = os.path.join(_PKG_DIR, "..", "remote")


TOOLS = [
    # ── SSH ──────────────────────────────────────────
    Tool(
        name="ssh_connect",
        description="SSH connect using a named configuration from config.yaml. "
                    "Auto-detects remote OS (Linux/Windows). "
                    "Supports auto-reconnect on connection loss. "
                    "Use list_connections to see available config entries.",
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "Unique session ID (reusing overwrites old)",
                },
                "config_name": {
                    "type": "string",
                    "description": "Name of the connection in config.yaml (e.g. 'windows-pc')",
                },
                "max_retries": {
                    "type": "integer",
                    "description": "Max auto-reconnect attempts (default 3)",
                    "default": 3,
                },
            },
            "required": ["session_id", "config_name"],
        },
    ),
    Tool(
        name="ssh_execute",
        description="Execute command on a connected SSH session. "
                    "Automatically adapts to Linux (bash) or Windows "
                    "(PowerShell) remote shell. Triggers auto-reconnect "
                    "if connection dropped.",
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "Session ID from ssh_connect",
                },
                "command": {
                    "type": "string",
                    "description": "Command to execute",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout seconds (default 30)",
                    "default": 30,
                },
            },
            "required": ["session_id", "command"],
        },
    ),
    Tool(
        name="ssh_disconnect",
        description="Close and remove an SSH session.",
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "Session ID"},
            },
            "required": ["session_id"],
        },
    ),
    Tool(
        name="ssh_upload",
        description="Upload file via SCP or SFTP. Automatically adapts "
                    "path format for Linux/Windows targets.",
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "Session ID"},
                "local_path": {"type": "string", "description": "Local file path"},
                "remote_path": {"type": "string", "description": "Remote destination"},
            },
            "required": ["session_id", "local_path", "remote_path"],
        },
    ),
    Tool(
        name="ssh_download",
        description="Download file via SCP or SFTP. Automatically adapts "
                    "path format for Linux/Windows targets.",
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "Session ID"},
                "remote_path": {"type": "string", "description": "Remote file path"},
                "local_path": {"type": "string", "description": "Local destination"},
            },
            "required": ["session_id", "remote_path", "local_path"],
        },
    ),
    Tool(
        name="ssh_list",
        description="List all active SSH sessions with platform and status.",
        inputSchema={"type": "object", "properties": {}},
    ),
    # ── Telnet ───────────────────────────────────────
    Tool(
        name="telnet_connect",
        description="Connect to remote host via Telnet, persistent background "
                    "session. All connection parameters come from the com2tcp "
                    "config in config.yaml.\n\n"
                    "Look up com2tcp config by name, resolve host from linked "
                    "SSH config, and use configured telnet_port, timeout, "
                    "buffer settings, and auto-reconnect policy.\n\n"
                    "Use list_connections to see available configs. "
                    "Use save_config to create/update com2tcp entries.",
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "Unique session ID (reusing overwrites old)",
                },
                "config_name": {
                    "type": "string",
                    "description": "Name of the com2tcp config in config.yaml "
                                   "(e.g. 'com2tcp_COM4_5200'). All connection "
                                   "params are read from this config.",
                },
            },
            "required": ["session_id", "config_name"],
        },
    ),
    Tool(
        name="telnet_send",
        description="Send data to a Telnet session. timeout=0: send and "
                    "return immediately (no wait). timeout>0: send then "
                    "wait for response. Supports auto-reconnect. "
                    "Special values: __CTRL_C__, __CTRL_D__, __CTRL_Z__.",
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "Session ID"},
                "data": {"type": "string", "description": "Data/command to send"},
                "timeout": {
                    "type": "integer",
                    "description": "Wait timeout seconds. 0 = no wait (default), "
                                   ">0 = wait for response",
                    "default": 0,
                },
            },
            "required": ["session_id", "data"],
        },
    ),
    Tool(
        name="telnet_listen",
        description="Listen on a Telnet session for a specified duration, "
                    "returning all newly received data (consumer pattern: "
                    "data is consumed after read). Supports multiple encodings "
                    "for binary data.",
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "Session ID"},
                "duration": {
                    "type": "integer",
                    "description": "Listen duration seconds (default 10)",
                    "default": 10,
                },
                "encoding": {
                    "type": "string",
                    "enum": ["utf-8", "base64", "hex"],
                    "description": "Output encoding: utf-8 (text), "
                                   "base64 (binary safe), hex (binary safe)",
                    "default": "utf-8",
                },
            },
            "required": ["session_id"],
        },
    ),
    Tool(
        name="telnet_disconnect",
        description="Close and remove a Telnet session.",
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "Session ID"},
            },
            "required": ["session_id"],
        },
    ),
    Tool(
        name="telnet_list",
        description="List all active Telnet sessions with status and "
                    "buffer sizes.",
        inputSchema={"type": "object", "properties": {}},
    ),
    # ── Workflow ─────────────────────────────────────
    Tool(
        name="setup_com2tcp",
        description="Complete com2telnet deployment workflow:\n"
                    "1. SSH upload com2telnet.py + pyproject.toml to "
                    "D:\\remote-debug\\com2telnet\\ on Windows PC\n"
                    "2. Install com2telnet + dependencies (pyserial) via pip\n"
                    "3. Kill any previous com2telnet instance on the same port\n"
                    "4. Start com2telnet in background "
                    "(PowerShell Start-Process -WindowStyle Hidden)\n"
                    "5. Verify process is running\n\n"
                    "After setup, use telnet_connect to host:telnet_port "
                    "to access serial data from the COM port.\n\n"
                    "Example: setup_com2tcp with com_port='COM4', "
                    "telnet_port=5200 runs:\n"
                    "  com2telnet --serial COM4:5200:115200",
        inputSchema={
            "type": "object",
            "properties": {
                "ssh_session_id": {
                    "type": "string",
                    "description": "SSH session ID (must already be connected "
                                   "to the Windows PC with the COM port)",
                },
                "com_port": {
                    "type": "string",
                    "description": "COM port name (e.g. COM4)",
                },
                "telnet_port": {
                    "type": "integer",
                    "description": "Telnet port to expose (e.g. 5200)",
                },
                "baud": {
                    "type": "integer",
                    "description": "Baud rate (default 115200)",
                    "default": 115200,
                },
            },
            "required": ["ssh_session_id", "com_port", "telnet_port"],
        },
    ),
    # ── Config ──────────────────────────────────────
    Tool(
        name="list_connections",
        description="List all configured connections from config.yaml. "
                    "Shows SSH and com2tcp entries.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="save_config",
        description="Save configuration to config.yaml. "
                    "This is the unified entry for creating/updating connection configs. "
                    "When no config.yaml exists: ask user for connection details "
                    "(host, port, username, password for SSH; ssh_config_name, com_port, "
                    "telnet_port for com2tcp), then call this tool with 'connections' parameter. "
                    "When setup_com2tcp completes, call this tool with the com2tcp connection "
                    "details to persist them. "
                    "When called without arguments, saves current in-memory config.",
        inputSchema={
            "type": "object",
            "properties": {
                "connections": {
                    "type": "array",
                    "description": "Connections to save/merge. "
                                  "type=ssh: {name, host, port, username, password, key_file?}. "
                                  "type=com2tcp: {name, ssh, com_port, telnet_port, baud, "
                                  "connect_timeout?, buffer_max_size?, max_retries?, username?, password?}. "
                                  "Omit to save current in-memory config.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "Connection name"},
                            "type": {"type": "string", "enum": ["ssh", "com2tcp"]},
                            "host": {"type": "string"},
                            "port": {"type": "integer"},
                            "username": {"type": "string"},
                            "password": {"type": "string"},
                            "key_file": {"type": "string"},
                            "ssh": {"type": "string", "description": "SSH config name (com2tcp only)"},
                            "com_port": {"type": "string", "description": "COM port name (com2tcp only)"},
                            "telnet_port": {"type": "integer", "description": "Telnet port (com2tcp only)"},
                            "baud": {"type": "integer", "description": "Baud rate (com2tcp only)"},
                            "connect_timeout": {"type": "integer", "description": "Connect timeout seconds (com2tcp, default 15)"},
                            "buffer_max_size": {"type": "integer", "description": "Buffer max size bytes (com2tcp, default 65536)"},
                            "max_retries": {"type": "integer", "description": "Max auto-reconnect attempts (com2tcp, default 3)"},
                        },
                        "required": ["name", "type"],
                    },
                },
            },
        },
    ),
    # ── Telnet Monitor ────────────────────────────────
    Tool(
        name="telnet_start_monitor",
        description="Start background monitoring on a Telnet session. "
                    "Data is continuously read into a circular buffer "
                    "(max 900000 lines, oldest evicted when full). "
                    "Optionally append to a file continuously.",
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "Telnet session ID",
                },
                "output_file": {
                    "type": "string",
                    "description": "Optional local file path to continuously "
                                   "append received data",
                },
            },
            "required": ["session_id"],
        },
    ),
    Tool(
        name="telnet_stop_monitor",
        description="Stop background monitoring on a Telnet session. "
                    "Returns the total line count captured.",
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "Telnet session ID",
                },
            },
            "required": ["session_id"],
        },
    ),
    # ── Utility ──────────────────────────────────────
    Tool(
        name="list_sessions",
        description="List all active SSH and Telnet sessions.",
        inputSchema={"type": "object", "properties": {}},
    ),
]


@server.list_tools()
async def list_tools():
    return TOOLS


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    mgr = get_manager()
    loop = asyncio.get_event_loop()

    try:
        if name == "ssh_connect":
            config_name = arguments["config_name"]
            session_id = arguments.get("session_id", config_name)
            result = await _ssh_connect_from_config(
                mgr, config_name, session_id,
                arguments.get("max_retries", 3),
            )

        elif name == "ssh_execute":
            result = await loop.run_in_executor(
                None,
                mgr.ssh_execute,
                arguments["session_id"],
                arguments["command"],
                arguments.get("timeout", 30),
            )

        elif name == "ssh_disconnect":
            result = mgr.ssh_disconnect(arguments["session_id"])

        elif name == "ssh_upload":
            result = await loop.run_in_executor(
                None,
                mgr.ssh_upload,
                arguments["session_id"],
                arguments["local_path"],
                arguments["remote_path"],
            )

        elif name == "ssh_download":
            result = await loop.run_in_executor(
                None,
                mgr.ssh_download,
                arguments["session_id"],
                arguments["remote_path"],
                arguments["local_path"],
            )

        elif name == "ssh_list":
            result = mgr.ssh_list()

        elif name == "telnet_connect":
            host, port, username, password, timeout, buf_size, retries = \
                _resolve_telnet_config(arguments["config_name"])
            result = await loop.run_in_executor(
                None,
                mgr.telnet_connect,
                arguments["session_id"],
                host,
                port,
                username,
                password,
                timeout,
                buf_size,
                retries,
            )

        elif name == "telnet_send":
            timeout = arguments.get("timeout", 0)
            if timeout > 0:
                result = await loop.run_in_executor(
                    None,
                    mgr.telnet_send,
                    arguments["session_id"],
                    arguments["data"],
                    timeout,
                )
            else:
                result = mgr.telnet_send(
                    arguments["session_id"],
                    arguments["data"],
                    timeout,
                )

        elif name == "telnet_listen":
            result = await loop.run_in_executor(
                None,
                mgr.telnet_listen,
                arguments["session_id"],
                arguments.get("duration", 10),
                arguments.get("encoding", "utf-8"),
            )

        elif name == "telnet_disconnect":
            result = mgr.telnet_disconnect(arguments["session_id"])

        elif name == "telnet_list":
            result = mgr.telnet_list()

        elif name == "setup_com2tcp":
            result = await _setup_com2tcp(
                mgr,
                arguments["ssh_session_id"],
                arguments["com_port"],
                arguments["telnet_port"],
                arguments.get("baud", 115200),
            )

        elif name == "telnet_start_monitor":
            result = mgr.telnet_start_monitor(
                arguments["session_id"],
                arguments.get("output_file", ""),
            )

        elif name == "telnet_stop_monitor":
            result = mgr.telnet_stop_monitor(arguments["session_id"])

        elif name == "list_sessions":
            result = mgr.list_all()

        elif name == "list_connections":
            result = await _list_connections()

        elif name == "save_config":
            result = await _save_config(arguments.get("connections"))

        else:
            result = f"Unknown tool: {name}"

        return [TextContent(type="text", text=str(result))]

    except Exception as e:
        return [TextContent(type="text", text=f"Error in {name}: {e}")]


async def _ssh_connect_from_config(mgr, config_name: str,
                                     session_id: str,
                                     max_retries: int) -> str:
    loop = asyncio.get_event_loop()
    try:
        config = get_config()
    except Exception as e:
        return f"Config load failed: {e}"

    entry = config.get_ssh(config_name)
    if not entry:
        return f"SSH config '{config_name}' not found. Use list_connections."

    if entry.key_file:
        result = await loop.run_in_executor(
            None, mgr.ssh_connect_key,
            session_id, entry.host, entry.port,
            entry.username, entry.key_file, max_retries,
        )
    else:
        result = await loop.run_in_executor(
            None, mgr.ssh_connect,
            session_id, entry.host, entry.port,
            entry.username, entry.password, max_retries,
        )
    return result


async def _list_connections() -> str:
    try:
        config = get_config()
    except Exception as e:
        return f"Config not loaded: {e}"

    lines = ["Configured connections:"]
    if config.ssh_connections:
        lines.append("\n[SSH]")
        for c in config.ssh_connections:
            lines.append(
                f"  {c.name}: {c.username}@{c.host}:{c.port}"
                + (" (key)" if c.key_file else "")
            )
    if config.com2tcp_connections:
        lines.append("\n[com2tcp]")
        for c in config.com2tcp_connections:
            extra = []
            if c.username:
                extra.append(f"login={c.username}")
            if c.connect_timeout != 15:
                extra.append(f"timeout={c.connect_timeout}s")
            if c.buffer_max_size != 65536:
                extra.append(f"buf={c.buffer_max_size}")
            if c.max_retries != 3:
                extra.append(f"retries={c.max_retries}")
            extra_str = " " + " ".join(extra) if extra else ""
            lines.append(
                f"  {c.name}: SSH={c.ssh} COM={c.com_port} "
                f"telnet=:{c.telnet_port} baud={c.baud}{extra_str}"
            )
    if not config.ssh_connections and not config.com2tcp_connections:
        lines.append("  (none)")
    return "\n".join(lines)


def _resolve_telnet_config(config_name: str):
    """从 com2tcp 配置解析全部连接参数。
    返回 (host, port, username, password, timeout, buffer_max_size, max_retries)。"""
    config = get_config()
    c2t = config.get_com2tcp(config_name)
    if not c2t:
        raise ValueError(
            f"com2tcp config '{config_name}' not found. "
            f"Use list_connections to see available configs."
        )
    ssh_cfg = config.get_ssh(c2t.ssh)
    if not ssh_cfg:
        raise ValueError(
            f"SSH config '{c2t.ssh}' (referenced by com2tcp '{config_name}') "
            f"not found. Check config.yaml."
        )
    return (ssh_cfg.host, c2t.telnet_port, c2t.username, c2t.password,
            c2t.connect_timeout, c2t.buffer_max_size, c2t.max_retries)


async def _save_config(connections=None) -> str:
    from remote_debug_mcp.config_loader import AppConfig, SSHConfig, Com2TcpConfig

    try:
        try:
            config = get_config()
        except FileNotFoundError:
            config = AppConfig()

        if connections:
            for c in connections:
                entry_type = c.get("type", "ssh")
                if entry_type == "ssh":
                    ssh_cfg = SSHConfig(
                        name=c.get("name", ""),
                        host=c.get("host", ""),
                        port=c.get("port", 22),
                        username=c.get("username", ""),
                        password=c.get("password", ""),
                        key_file=c.get("key_file", ""),
                    )
                    existing = config.get_ssh(ssh_cfg.name)
                    if existing:
                        existing.host = ssh_cfg.host
                        existing.port = ssh_cfg.port
                        existing.username = ssh_cfg.username
                        existing.password = ssh_cfg.password
                        existing.key_file = ssh_cfg.key_file
                    else:
                        config.ssh_connections.append(ssh_cfg)
                elif entry_type == "com2tcp":
                    c2t_cfg = Com2TcpConfig(
                        name=c.get("name", ""),
                        ssh=c.get("ssh", ""),
                        com_port=c.get("com_port", ""),
                        telnet_port=c.get("telnet_port", 5200),
                        baud=c.get("baud", 115200),
                        username=c.get("username", ""),
                        password=c.get("password", ""),
                        connect_timeout=c.get("connect_timeout", 15),
                        buffer_max_size=c.get("buffer_max_size", 65536),
                        max_retries=c.get("max_retries", 3),
                    )
                    existing = config.get_com2tcp(c2t_cfg.name)
                    if existing:
                        existing.ssh = c2t_cfg.ssh
                        existing.com_port = c2t_cfg.com_port
                        existing.telnet_port = c2t_cfg.telnet_port
                        existing.baud = c2t_cfg.baud
                        existing.username = c2t_cfg.username
                        existing.password = c2t_cfg.password
                        existing.connect_timeout = c2t_cfg.connect_timeout
                        existing.buffer_max_size = c2t_cfg.buffer_max_size
                        existing.max_retries = c2t_cfg.max_retries
                    else:
                        config.com2tcp_connections.append(c2t_cfg)

        result = save_config(config)
        return result
    except Exception as e:
        return f"Save config failed: {e}"


async def _setup_com2tcp(mgr, ssh_session_id: str, com_port: str,
                         telnet_port: int, baud: int) -> str:
    loop = asyncio.get_event_loop()

    com2telnet_py = os.path.join(REMOTE_TOOLS_DIR, "com2telnet.py")
    pyproject_toml = os.path.join(REMOTE_TOOLS_DIR, "pyproject.toml")

    for f in [com2telnet_py, pyproject_toml]:
        if not os.path.exists(f):
            return f"Required file not found: {f}"

    remote_dir = "D:\\remote-debug\\com2telnet"
    remote_com2telnet = f"{remote_dir}\\com2telnet.py"
    remote_pyproject = f"{remote_dir}\\pyproject.toml"

    parts = [
        f"=== com2telnet deployment ===",
        f"SSH session : {ssh_session_id}",
        f"COM port    : {com_port}",
        f"Telnet port : {telnet_port}",
        f"Baud rate   : {baud}",
        "",
    ]

    mkdir_cmd = f"New-Item -ItemType Directory -Force -Path {remote_dir} | Out-Null"
    await loop.run_in_executor(None, mgr.ssh_execute, ssh_session_id, mkdir_cmd, 5)
    parts.append(f"[Mkdir] {remote_dir}")

    upload1 = await loop.run_in_executor(
        None, mgr.ssh_upload, ssh_session_id,
        com2telnet_py, remote_com2telnet,
    )
    parts.append(f"[Upload com2telnet.py] {upload1}")

    upload2 = await loop.run_in_executor(
        None, mgr.ssh_upload, ssh_session_id,
        pyproject_toml, remote_pyproject,
    )
    parts.append(f"[Upload pyproject.toml] {upload2}")

    if "OK" not in upload1:
        return "\n".join(parts)

    parts.append("")

    pip_cmd = f"python -m pip install pyserial 2>&1 | Select-Object -Last 3"
    pip_output = await loop.run_in_executor(
        None, mgr.ssh_execute, ssh_session_id,
        pip_cmd, 30,
    )
    parts.append(f"[pip check] {pip_output.strip() or '(pyserial OK)'}")

    kill_cmd = (
        f"$pids = (netstat -ano | Select-String ':{telnet_port}.*LISTENING' | "
        f"ForEach-Object {{ ($_ -split '\\s+')[-1] }} | "
        f"Where-Object {{ $_ -match '^\\d+$' }}); "
        f"if ($pids) {{ foreach ($p in $pids) {{ taskkill /PID $p /F 2>&1 }} }}"
    )
    kill_output = await loop.run_in_executor(
        None, mgr.ssh_execute, ssh_session_id, kill_cmd, 10,
    )
    parts.append(f"[Kill previous] {kill_output.strip() if kill_output.strip() else '(none)'}")

    launch_cmd = (
        f"Start-Process -WindowStyle Hidden -FilePath python "
        f"-ArgumentList "
        f"'{remote_com2telnet}',"
        f"'--serial','{com_port}:{telnet_port}:{baud}'"
    )
    parts.append(f"[Launch] {launch_cmd}")

    launch_output = await loop.run_in_executor(
        None, mgr.ssh_execute, ssh_session_id, launch_cmd, 5,
    )
    parts.append(f"[Launch output] {launch_output.strip() if launch_output else '(launched)'}")

    await asyncio.sleep(2)

    verify_cmd = (
        f"netstat -ano | Select-String ':{telnet_port}.*LISTENING'"
    )
    pid_check = await loop.run_in_executor(
        None, mgr.ssh_execute, ssh_session_id, verify_cmd, 5,
    )
    parts.append(f"[Process check] {pid_check.strip() if pid_check and pid_check.strip() else '(no output - may still be starting)'}")

    session = mgr._ssh_sessions.get(ssh_session_id)
    host = session.params.host if session else "unknown"

    parts.append("")
    parts.append("=== Setup complete ===")
    parts.append(f"Connect: telnet_connect(session_id='serial', "
                 f"config_name='com2tcp_{com_port}_{telnet_port}')")
    parts.append("")
    parts.append("IMPORTANT: To persist this com2tcp config, call save_config with:")
    s = (f"  connections=[{{\"name\":\"com2tcp_{com_port}_{telnet_port}\","
         f"\"type\":\"com2tcp\",\"ssh\":\"<your-ssh-config-name>\","
         f"\"com_port\":\"{com_port}\",\"telnet_port\":{telnet_port},"
         f"\"baud\":{baud}}}]")
    parts.append(s)

    return "\n".join(parts)


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )
