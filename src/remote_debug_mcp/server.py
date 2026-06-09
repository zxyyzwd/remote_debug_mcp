import os
import asyncio

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from remote_debug_mcp.sessions import get_manager
from remote_debug_mcp.config_loader import load_config, get_config, reload_config, save_config

server = Server("remote-debug-mcp")

COM2TCP_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "com2tcp.exe")


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
                    "session. Supports optional login. Maintains an internal "
                    "buffer for accumulated data (configurable size).",
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "Unique session ID",
                },
                "host": {"type": "string", "description": "Remote host IP/hostname"},
                "port": {
                    "type": "integer",
                    "description": "Telnet port (default 23)",
                    "default": 23,
                },
                "username": {
                    "type": "string",
                    "description": "Login username (optional)",
                },
                "password": {
                    "type": "string",
                    "description": "Login password (optional)",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Connection timeout seconds (default 15)",
                    "default": 15,
                },
                "buffer_max_size": {
                    "type": "integer",
                    "description": "Max buffer size in bytes (default 65536 = 64KB)",
                    "default": 65536,
                },
                "max_retries": {
                    "type": "integer",
                    "description": "Max auto-reconnect attempts (default 3)",
                    "default": 3,
                },
            },
            "required": ["session_id", "host"],
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
        description="Complete com2tcp workflow:\n"
                    "1. SSH upload com2tcp.exe to Windows PC via base64\n"
                    "2. Kill any previous com2tcp instance on the same port\n"
                    "3. Start com2tcp in background (PowerShell Start-Process)\n"
                    "4. Verify process is running\n"
                    "5. Returns telnet connection info\n\n"
                    "After setup, use telnet_connect to host:telnet_port "
                    "to access serial data from the COM port.\n\n"
                    "Example: setup_com2tcp with com_port='COM4', "
                    "telnet_port=5200 runs:\n"
                    "  com2tcp.exe --telnet --ignore-dsr --baud 115200 COM4 5200",
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
        description="Save current in-memory configuration to config.yaml.",
        inputSchema={"type": "object", "properties": {}},
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
            result = await loop.run_in_executor(
                None,
                mgr.telnet_connect,
                arguments["session_id"],
                arguments["host"],
                arguments.get("port", 23),
                arguments.get("username", ""),
                arguments.get("password", ""),
                arguments.get("timeout", 15),
                arguments.get("buffer_max_size", 65536),
                arguments.get("max_retries", 3),
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
            result = await _save_config()

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
            lines.append(
                f"  {c.name}: SSH={c.ssh} COM={c.com_port} "
                f"telnet=:{c.telnet_port} baud={c.baud}"
            )
    if not config.ssh_connections and not config.com2tcp_connections:
        lines.append("  (none)")
    return "\n".join(lines)


async def _save_config() -> str:
    try:
        config = get_config()
        result = save_config(config)
        return result
    except Exception as e:
        return f"Save config failed: {e}"


async def _setup_com2tcp(mgr, ssh_session_id: str, com_port: str,
                         telnet_port: int, baud: int) -> str:
    loop = asyncio.get_event_loop()

    if not os.path.exists(COM2TCP_PATH):
        return f"com2tcp.exe not found at {COM2TCP_PATH}"

    exe_name = f"com2tcp_{telnet_port}.exe"
    remote_exe_path = f"D:\\remote_debug\\{exe_name}"

    parts = [
        f"=== com2tcp setup ===",
        f"SSH session : {ssh_session_id}",
        f"COM port    : {com_port}",
        f"Telnet port : {telnet_port}",
        f"Baud rate   : {baud}",
        "",
    ]

    upload_result = await loop.run_in_executor(
        None,
        mgr.ssh_upload,
        ssh_session_id,
        COM2TCP_PATH,
        remote_exe_path,
    )
    parts.append(f"[Upload] {upload_result}")

    if "uploaded" not in upload_result.lower():
        return "\n".join(parts)

    parts.append("")

    kill_cmd = f"taskkill /F /IM {exe_name} 2>$null"
    kill_output = await loop.run_in_executor(
        None, mgr.ssh_execute, ssh_session_id, kill_cmd, 5,
    )
    parts.append(f"[Kill previous] {kill_output.strip()}")

    launch_cmd = (
        f'Start-Process -WindowStyle Hidden -FilePath '
        f'"{remote_exe_path}" -ArgumentList '
        f'\'--telnet\',\'--ignore-dsr\',\'--baud\',\'{baud}\','
        f'\'{com_port}\',\'{telnet_port}\''
    )
    parts.append(f"[Launch] {launch_cmd}")

    launch_output = await loop.run_in_executor(
        None, mgr.ssh_execute, ssh_session_id, launch_cmd, 5,
    )
    parts.append(f"[Launch output] {launch_output.strip()}")

    await asyncio.sleep(1)

    pid_check = await loop.run_in_executor(
        None,
        mgr.ssh_execute,
        ssh_session_id,
        f'Get-Process -Name "{exe_name.replace(".exe", "")}" '
        f'-ErrorAction SilentlyContinue | '
        f'Select-Object Id, ProcessName | Format-List',
        5,
    )
    parts.append(f"[Process check] {pid_check.strip() if pid_check else '(no output - may still be starting)'}")

    session = mgr._ssh_sessions.get(ssh_session_id)
    host = session.params.host if session else "unknown"

    parts.append("")
    parts.append("=== Setup complete ===")
    parts.append(f"Telnet: telnet_connect(session_id='com2tcp_{telnet_port}', "
                 f"host='{host}', port={telnet_port})")

    return "\n".join(parts)


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )
