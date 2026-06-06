# AGENTS.md

## 项目

基于 MCP 的远程调试服务器，支持 SSH 和 Telnet 后台持久化连接。Python + pexpect 实现。

## 命令

```bash
# 可编辑模式安装
pip install -e .

# 启动 MCP 服务器（stdio 传输，供 MCP 客户端调用）
python -m remote_debug_mcp
```

暂无 lint、typecheck、测试命令配置。

## 架构

```
src/remote_debug_mcp/
├── __init__.py     # 导出 main()
├── __main__.py     # asyncio.run(main())
├── server.py       # MCP 服务端：18 个工具定义 + 分发
└── sessions.py     # SessionManager：SSH/Telnet 连接生命周期管理
```

- **入口**: `remote_debug_mcp.server.main()` — 启动 MCP stdio 服务器。
- **SessionManager** (`sessions.py`): 通过 `get_manager()` 获取单例。持有 `SSHSession` 和 `TelnetSession` 字典，按调用方自选的 `session_id` 索引。所有 pexpect I/O 均为同步操作，通过 `loop.run_in_executor` 在线程池中执行。
- **SSH 会话** 使用 `pexpect.spawn('ssh', ...)` 建立原始连接（**不使用 pxssh**，以兼容 Windows cmd/PowerShell 提示符）。连接时自动检测远程平台（Linux/Windows）并统一采用「命令追加 echo 标记」策略分隔输出，不依赖 shell 提示符同步。
- **Telnet 会话** 使用原始 `pexpect.spawn('telnet', ...)`，支持可选的登录提示检测。
- **工具** 定义为 `server.py` 中模块级的 `TOOLS` 列表；`call_tool` 按名称分发到 `SessionManager` 方法。
- **DESIGN.md** 包含完整架构规格说明。

## SSH 命令执行策略

**统一策略**：所有目标（Linux bash / Windows cmd / PowerShell）均使用同一方式：

```
child.sendline(f"{command}; echo '__MCP_CMD_{timestamp}__'")
child.expect("__MCP_CMD_{timestamp}__", timeout)
output = child.before
```

每次命令带唯一时间戳标记，不依赖 shell 提示符。彻底避免 `pexpect.pxssh` 在 Windows cmd 提示符下 "could not synchronize with original prompt" 的问题。

## 平台检测

连接后发送 `echo __MCP_PLATFORM_DETECT__ && uname -s 2>/dev/null || echo __WINDOWS__ && echo __MCP_DETECT_DONE__`，根据输出中是否含 `__WINDOWS__` 判定 linux/windows。

## 关键约定

- 会话 ID 由调用方自行选择字符串。重复使用同一 ID 会覆盖旧会话。
- 所有同步 pexpect 调用必须通过 `loop.run_in_executor` 执行，避免阻塞 asyncio 事件循环。
- `SSHSession` 存储 `ConnectionParams`（主机、端口、用户名、密码/密钥），供自动重连使用。
- `TelnetSession` 维护一个字节缓冲区，带读指针；`telnet_read` / `telnet_listen` 消费新数据，`telnet_read_all` 清空全部缓冲区。缓冲区有可配置的最大大小（默认 64KB）。
- `telnet_listen` / `telnet_read` / `telnet_read_all` 支持 `encoding` 参数：`utf-8`（默认）、`base64` 或 `hex`，用于安全处理二进制数据。
- `com2tcp.exe` 位于仓库根目录。`setup_com2tcp` 通过 `ssh_upload_binary`（SSH 内 base64 编码）上传，通过 PowerShell `Start-Process -WindowStyle Hidden` 后台启动，之后调用方可 `telnet_connect` 到暴露的端口。

## 自动重连

SSH 和 Telnet 会话均支持连接断开后自动重连。可通过 `max_retries`（默认 3）和指数退避进行配置。重连参数存储在各会话的 `ConnectionParams` 中。

## 注意事项

- **不使用 pexpect.pxssh**。Windows cmd/PowerShell 的提示符格式与 Unix shell 不同，pxssh 的 `sync_original_prompt()` 在 Windows 上必然失败。
- Base64 二进制上传按 32000 字符分块；大文件虽慢但可靠。
- Telnet `telnet_listen` 会在线程池中阻塞完整的 `duration` 秒 — 保持合理的持续时间（≤ 60s）。
- SCP 传输（`ssh_upload` / `ssh_download`）仅对 Linux 目标可靠。Windows 目标请使用 `ssh_upload_binary`。
- 在 Windows SSH 目标上，过长的 PowerShell 命令行可能会被截断。二进制上传的块大小已考虑此限制。
