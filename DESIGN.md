# Remote Debug MCP — 设计文档

## 1. 概述

基于 **MCP (Model Context Protocol)** 构建的远程调试服务器，通过 **SSH** 和 **Telnet** 提供后台持久化连接。使用 **Python + pexpect** 实现，通过 stdio 传输与 MCP 客户端（如 Claude Desktop、OpenCode 等）通信。

### 核心能力

```
┌─────────────────────────────────────────────────┐
│                  MCP Client                      │
│         (Claude Desktop / OpenCode)              │
└─────────────────┬───────────────────────────────┘
                  │ stdio (JSON-RPC)
┌─────────────────▼───────────────────────────────┐
│              remote-debug-mcp                     │
│  ┌───────────────────────────────────────────┐  │
│  │              server.py                     │  │
│  │   • 17 MCP 工具定义 & 注册                 │  │
│  │   • call_tool 分发 → SessionManager        │  │
│  └─────────────────┬─────────────────────────┘  │
│                    │                              │
│  ┌─────────────────▼─────────────────────────┐  │
│  │            sessions.py                     │  │
│  │  ┌──────────┐  ┌──────────┐  ┌─────────┐  │  │
│  │  │SSHSession│  │ Telnet   │  │ com2tcp  │  │  │
│  │  │ (spawn)  │  │ (spawn)  │  │ workflow │  │  │
│  │  └────┬─────┘  └────┬─────┘  └────┬─────┘  │  │
│  │       │              │              │        │  │
│  │  loop.run_in_executor (线程池)               │  │
│  └───────┼──────────────┼──────────────┼────────┘  │
│          │              │              │            │
└──────────┼──────────────┼──────────────┼────────────┘
           │              │              │
    ┌──────▼──────┐ ┌─────▼──────┐ ┌────▼──────────┐
    │ Remote Linux │ │  Remote    │ │ Remote Windows │
    │   Server     │ │  Telnet    │ │    (COM port)  │
    └─────────────┘ └────────────┘ └───────────────┘
```

---

## 2. 连接架构

### 2.1 会话生命周期

```
                    ┌──────────────┐
          connect──▶│ connecting   │
                    └──────┬───────┘
                           │ auth OK
                    ┌──────▼───────┐
                    │  connected   │◄──── reconnect ────┐
                    └──┬───────┬───┘                    │
          execute cmd  │       │ connection lost        │
          (复用会话)    │       ▼                        │
                    ┌──▼──────────────┐    retry OK     │
                    │  disconnected   │─────────────────┘
                    └─────────────────┘
                           │
                    disconnect (主动)
                           ▼
                    ┌─────────────────┐
                    │  closed/removed │
                    └─────────────────┘
```

- **Session ID**: 调用方自选字符串，重复使用会覆盖旧会话
- **会话状态**: `connecting` → `connected` → `disconnected`
- **自动重连**: 连接断开后自动重试（最多 N 次，指数退避），重连成功后恢复 `connected` 状态

### 2.2 连接参数保存

每次 connect 时保存完整连接参数，供自动重连使用：

```python
@dataclass
class ConnectionParams:
    host: str
    port: int
    username: str
    password: str       # SSH 密码（明文内存）
    key_file: str       # SSH 密钥路径
    connect_timeout: int
    max_retries: int = 3
    retry_backoff: float = 2.0  # 退避倍数
```

---

## 3. SSH 子系统设计

### 3.1 平台适配与命令执行

**不使用 pexpect.pxssh**，改为原始 `pexpect.spawn('ssh', ...)`。pxssh 的 `sync_original_prompt()` 和 `login()` 依赖 Unix shell 提示符格式，在 Windows cmd（提示符如 `C:\Users\xxx>$P$G`）或 PowerShell 下必然失败，报错 "could not synchronize with original prompt"。

**命令执行策略**：每次命令追加唯一 echo 标记，收集原始字节，按平台分编码解码输出。

```
Linux / Windows 统一方式:
  marker = f"__MCP_CMD_{timestamp}__"
  child.send(f"{command}; echo {marker}".encode(platform_encoding) + b"\n")
  收集后按编码解码: output = raw.decode(platform_encoding)
```

目标系统分两种，连接后自动检测：

| 平台 | Shell | 检测方式 | 命令编码 | 输出解码 |
|------|-------|---------|---------|---------|
| **Linux** | bash/sh | `echo __MCP_PLATFORM_DETECT__ && uname -s ... ` | UTF-8 | UTF-8 |
| **Windows** | CMD → PowerShell | 同上，输出中含 `__WINDOWS__` 则判定 | GBK | GBK |

连接后自动从 CMD 切换到 PowerShell（`powershell` 命令），工作目录 `D:\remote_debug`。

### 3.2 中文编码处理

pexpect 使用 `encoding=None`（原始字节模式），所有编解码由应用层控制：

```
Windows 流程:
  ┌─────────┐     GBK 编码命令     ┌──────────────┐
  │  MCP    │ ──────────────────▶ │  PowerShell   │
  │  Server │                     │  (GBK 输入)    │
  │         │  ◀── 原始字节 ────   │  (GBK 输出)    │
  │         │  → GBK 解码输出     └──────────────┘
  └─────────┘

Linux 流程:
  ┌─────────┐     UTF-8 编码命令   ┌──────────────┐
  │  MCP    │ ──────────────────▶ │  bash/sh      │
  │  Server │                     │  (UTF-8)      │
  │         │  ◀── 原始字节 ────   │              │
  │         │  → UTF-8 解码输出   └──────────────┘
  └─────────┘
```

关键点：
- pexpect `encoding=None` 避免内部 UTF-8 解码损失 GBK 字节
- Windows 命令含中文参数时必须用 GBK 编码发送，否则路径无法识别
- 标记 `__MCP_CMD_<ts>__` 纯 ASCII，GBK/UTF-8 字节相同，平台无关

### 3.3 文件传输

| 方式 | 适用目标 | 原理 |
|------|---------|------|
| **SCP** | Linux + Windows | `pexpect.spawn('scp ...')`，处理密码提示 |
| **SFTP** | Linux + Windows | `pexpect.spawn('sftp ...')`，交互式批处理 |

**优先级**：SCP 优先 → SFTP 兜底。SCP 不支持含空格路径时自动降级到 SFTP。

**路径规范化**：Windows 目标自动将远程路径转为 `/D:/path/to/file` 格式（正斜杠 + 前缀斜杠），兼容 SCP 和 SFTP。

**SFTP 自动创建目录**：上传时若父目录不存在，SFTP 自动逐级 `mkdir` 创建（UTF-8 编码，兼容中文目录名）。

**MD5 校验**：上传/下载完成后自动计算本地与远程文件 MD5 并比较，结果以 `[MD5 OK: xxx]` / `[MD5 MISMATCH!]` / `[(MD5 verify skipped)]` 格式追加到传输结果尾部。

### 3.4 自动重连

```
执行命令
  │
  ├─ 成功 → 返回输出
  │
  └─ 失败 (EOF / connection reset)
       │
       ├─ retry_count < max_retries
       │    sleep(backoff * 2^retry_count)
       │    重新 _ssh_spawn() 连接
       │    ├─ 成功 → 重试原命令
       │    └─ 失败 → retry_count++
       │
       └─ retry_count >= max_retries
            → 标记 disconnected，返回错误
```

---

## 4. Telnet 子系统设计

### 4.1 连接流程

```
spawn('telnet', [host, port])
  │
  ├─ "Escape character is" → 连接成功（无登录）
  ├─ "login:" / "Username:" → 需要登录
  │     sendline(username)
  │     expect "Password:"
  │     sendline(password)
  │     expect 提示符
  └─ TIMEOUT / EOF → 连接失败
```

### 4.2 缓冲区管理（消费模式）

每个 Telnet 会话维护一个内部循环缓冲区：

```python
@dataclass
class TelnetSession:
    ...
    buffer: bytes = b""            # 循环缓冲区
    buffer_max_size: int = 65536  # 64KB 默认，可配置
    read_cursor: int = 0          # 读指针（已消费位置）
```

**数据流**：

```
远程串口 ──continuous──▶ com2tcp ──telnet──▶ TelnetSession.buffer
                                                    │
                          telnet_listen(duration)   │
                          ◄──────────────────────────┘
                          返回 buffer[read_cursor:] （新数据）
                          read_cursor 移到末尾
```

| 工具 | 行为 | 移动 cursor |
|------|------|------------|
| `telnet_listen` | 监听 duration 秒，返回期间收到的新数据 | 是 |

`telnet_read` / `telnet_read_all` 已删除，合并为 `telnet_listen` 覆盖。`telnet_send` 合并了 `telnet_execute`：`timeout=0` 发后即返，`timeout>0` 发后等响应。

### 4.3 二进制数据处理

串口数据可能包含非 UTF-8 字节。Telnet 缓冲区存储 **原始 bytes**，返回时提供两种编码选项：

- `encoding="utf-8"` (默认): 用 `errors="replace"` 解码为字符串
- `encoding="base64"`: 返回 base64 编码的原始字节
- `encoding="hex"`: 返回十六进制编码

### 4.4 后台持续监听

```
telnet_start_monitor(output_file?)
  │
  ├─ 启动 daemon 后台线程
  │    while monitor_active:
  │        data = child.read_nonblocking(4096)
  │        lines.append(data.split(b"\n"))
  │        if output_file: f.write(data)
  │
  ├─ 行列缓存: deque(maxlen=900000) FIFO 淘汰
  │
  ├─ telnet_send / read / listen 可并发使用
  │    io_lock 保护 PTY 读写
  │
  └─ telnet_stop_monitor → 停止线程，返回行数
```

monitor 运行时 `telnet_read` / `telnet_read_all` / `telnet_listen` 从 deque 取数据，非激活时沿用旧 buffer 路径。`telnet_send` 支持控制字符 `__CTRL_C__`（0x03）、`__CTRL_Z__`（0x1a）、`__CTRL_D__`（0x04）。

### 4.5 自动重连

Telnet 同样支持自动重连。重连时重新执行完整的连接 + 登录流程。

---

## 5. com2tcp 工作流

### 5.1 场景与配置链路

```
LLM 与 MCP 交互流程:
  无 config.yaml → LLM 询问用户参数 → save_config(connections=...)
  setup_com2tcp 完成 → save_config 持久化 com2tcp 配置
  telnet_connect(config_name="com2tcp_COM4_5200") → 自动解析 host/port

数据流:
┌──────────────┐     SSH (PowerShell)     ┌─────────────────────────┐
│  MCP Server  │ ───────────────────────▶ │  Windows PC (目标机)     │
│              │                          │                          │
│              │  1. upload com2tcp.exe   │  D:\remote_debug\        │
│              │  2. 后台启动 com2tcp      │    com2tcp_5200.exe       │
│              │                          │                          │
│              │     Telnet               │  COM4 ◄── 串口设备       │
│              │ ◄─────────────────────── │    :5200 (telnet)        │
└──────────────┘                          └─────────────────────────┘
```

### 5.2 执行步骤

```
1. 前置条件: SSH 已连接到 Windows PC (session_id),
   config.yaml 中已有 SSH 配置（名称如 "windows-pc"）。

2. 上传 com2tcp.exe
   setup_com2tcp 自动通过 SSH 上传 exe 到 D:\remote_debug\

3. 终止旧进程 + 后台启动 + 验证

4. 提示 LLM 调用 save_config 持久化 com2tcp 配置：
   save_config(connections=[{
     name: "com2tcp_COM4_5200", type: "com2tcp",
     ssh: "windows-pc", com_port: "COM4",
     telnet_port: 5200, baud: 115200
   }])

5. 后续连接无需传递 host/port，只需：
   telnet_connect(session_id="serial", config_name="com2tcp_COM4_5200")
   → 系统自动解析 host（从 SSH 配置）+ port（从 com2tcp 配置）
```

### 5.3 参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `baud` | 115200 | 串口波特率 |
| `--ignore-dsr` | 始终开启 | 忽略 DSR 信号 |
| `--telnet` | 始终开启 | Telnet 模式（非 RFC2217） |

### 5.4 Com2TcpConfig 数据模型

```python
@dataclass
class Com2TcpConfig:
    name: str                    # 配置名（e.g. "com2tcp_COM4_5200"）
    ssh: str                     # 关联的 SSH 配置名（用于解析 host）
    com_port: str                # COM 端口名
    telnet_port: int = 5200      # Telnet 端口
    baud: int = 115200           # 波特率
    username: str = ""           # Telnet 登录用户名
    password: str = ""           # Telnet 登录密码
    connect_timeout: int = 15    # 连接超时（秒）
    buffer_max_size: int = 65536 # 缓冲区大小（字节）
    max_retries: int = 3         # 自动重连次数
```

所有扩展字段均有默认值，向后兼容旧配置文件。

---

## 6. 线程模型

```
asyncio event loop (主线程)
  │
  ├─ stdio_server() ── MCP 协议消息循环
  │
  └─ call_tool(name, args) [async]
       │
       └─ loop.run_in_executor(None, sync_func, ...)
            │
            └─ ThreadPoolExecutor (默认线程池)
                 │
                 ├─ mgr.ssh_connect(...)      # 阻塞式 pexpect
                 ├─ mgr.ssh_execute(...)
                 ├─ mgr.telnet_connect(...)
                 └─ mgr.telnet_listen(...)    # 会阻塞 duration 秒
```

**关键约束**:
- 所有 `pexpect` 调用必须在 `run_in_executor` 中
- `telnet_listen` 会长时间占用线程，duration 不宜过大（建议 ≤ 60s）
- `SessionManager._lock` 保护 session dict 的并发访问

---

## 7. 错误处理

### 7.1 错误分类

| 类型 | 示例 | 处理 |
|------|------|------|
| 连接错误 | host unreachable, auth failed | 返回错误信息，不重连 |
| 传输错误 | connection reset, EOF | 触发自动重连逻辑 |
| 超时 | 命令执行超时 | 返回部分输出 + `[TIMEOUT]` 标记 |
| 协议错误 | 未知工具名 | 返回 `Unknown tool: {name}` |

### 7.2 返回格式

成功:
```
SSH connected: user@10.0.0.1:22 [session=myssh]
```

失败:
```
SSH connection failed [myssh]: Authentication failed
[TIMEOUT] Command exceeded 30s: long_running_task
```

---

## 8. 安全考虑

- 密码在内存中明文存储（`ConnectionParams.password`），进程终止后清除
- SSH `StrictHostKeyChecking=no` 跳过主机密钥验证（内网调试场景可接受）
- 不在日志中输出密码
- 建议生产环境使用密钥认证 (`ssh_connect_key`)

---

## 9. MCP 工具清单

### SSH (6 个)

| 工具 | 说明 | 关键参数 |
|------|------|---------|
| `ssh_connect` | 通过 config_name 从 config.yaml 读取参数连接（密码/密钥自适应） | session_id, config_name |
| `ssh_execute` | 执行命令（自动适配 bash/PowerShell，中文编码正确） | session_id, command, timeout |
| `ssh_upload` | SCP 上传（自动降级 SFTP，空格兼容，中文目录自动 mkdir，MD5 校验） | session_id, local_path, remote_path |
| `ssh_download` | SCP 下载（自动降级 SFTP，空格兼容，MD5 校验） | session_id, remote_path, local_path |
| `ssh_disconnect` | 关闭会话 | session_id |
| `ssh_list` | 列出会话 | — |

### Telnet (5 个) + 监控 (2 个)

| 工具 | 说明 | 关键参数 |
|------|------|---------|
| `telnet_connect` | 通过 com2tcp config_name 连接（host/port/login/buffer/retries 全部从配置解析，无需 LLM 传参） | session_id, config_name |
| `telnet_send` | 发送数据（timeout=0 发后即返，timeout>0 等响应；支持 `__CTRL_C__`/`__CTRL_D__`/`__CTRL_Z__`） | session_id, data, timeout |
| `telnet_listen` | 监听新数据（支持 utf-8/base64/hex 编码） | session_id, duration, encoding |
| `telnet_start_monitor` | 启动后台持续监听，可选文件输出 | session_id, output_file? |
| `telnet_stop_monitor` | 停止后台监听 | session_id |
| `telnet_disconnect` | 关闭会话 | session_id |
| `telnet_list` | 列出会话 | — |

### 工作流 (1 个)

| 工具 | 说明 |
|------|------|
| `setup_com2tcp` | SSH 上传 + 后台启动 com2tcp，返回 telnet 连接信息 |

### 配置 (2 个)

| 工具 | 说明 | 关键参数 |
|------|------|---------|
| `list_connections` | 列出已加载配置中的所有 SSH 和 com2tcp 连接 | — |
| `save_config` | **配置唯一入库入口**。无参：保存内存配置。带 `connections` 参数：合并条目后写入文件，支持 type=ssh `{name,host,port,username,password,key_file?}` 和 type=com2tcp `{name,ssh,com_port,telnet_port,baud,connect_timeout?,buffer_max_size?,max_retries?,username?,password?}`。无配置文件时自动创建 | connections? |

### 通用 (1 个)

| 工具 | 说明 |
|------|------|
| `list_sessions` | 列出所有 SSH 和 Telnet 会话 |

**总计: 17 个工具**

---

## 10. 测试策略

| 层级 | 内容 | 方式 |
|------|------|------|
| 单元测试 | SessionManager 状态管理、工具注册 | 本地可运行 |
| 集成测试 | SSH/Telnet 连接真实目标 | 需要测试环境 |
| 模拟测试 | Mock pexpect 验证流程 | 自动化 CI |

---

## 11. 后续扩展

- [ ] Telnet 数据流式推送（通过 MCP 通知）
- [ ] SSH 端口转发（本地/远程）
- [ ] 多 COM 端口同时桥接
- [ ] 会话心跳保活
- [ ] 连接日志持久化
- [x] ~~配置文件支持（YAML 预设连接参数）~~
- [x] ~~Base64 传输删除（仅保留 SCP + SFTP）~~
- [x] ~~中文编码处理（GBK/UTF-8 自适应）~~
