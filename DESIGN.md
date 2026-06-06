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
│  │   • 18 MCP 工具定义 & 注册                 │  │
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

**统一命令执行策略**（所有目标通用）：每次命令追加唯一 echo 标记，expect 等待标记出现，取 `child.before` 为输出。

```
Linux / Windows 统一方式:
  marker = f"__MCP_CMD_{timestamp}__"
  child.sendline(f"{command}; echo '{marker}'")
  child.expect(marker, timeout)
  output = child.before  # 标记前的全部内容即为命令输出
```

目标系统分两种，连接后自动检测：

| 平台 | Shell | 检测方式 |
|------|-------|---------|
| **Linux** | bash/sh | 连接后发送 `echo __MCP_PLATFORM_DETECT__ && uname -s 2>/dev/null \|\| echo __WINDOWS__ && echo __MCP_DETECT_DONE__`，输出中不含 `__WINDOWS__` 则判定为 linux |
| **Windows** | cmd / PowerShell | 同上，输出中含 `__WINDOWS__` 则判定为 windows |

### 3.2 文件传输

| 方式 | 适用目标 | 原理 |
|------|---------|------|
| **SCP** | Linux | `pexpect.spawn('scp ...')`，处理密码提示 |
| **Base64 内联** | Linux + Windows | 读文件 → base64 → 分块 printf → 远程解码 |

**Base64 分块上传流程**：

```
本地                         远程 (SSH shell)
────                         ────────────────
读二进制文件
base64 编码 (3→4 膨胀)
│
├─ 第1块(32000 char) ──▶  printf '%s' '...' > file.b64
├─ 第2块(32000 char) ──▶  printf '%s' '...' >> file.b64
├─ ...
└─ 最后一块 ──────────▶  printf '%s' '...' >> file.b64

解码:
  Linux:   base64 -d file.b64 > file.exe && rm file.b64
  Windows: certutil -decode file.b64 file.exe && del file.b64

验证:
  Linux:   wc -c < file.exe
  Windows: (Get-Item 'file.exe').Length
```

### 3.3 自动重连

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
    buffer: str = ""              # 循环缓冲区
    buffer_max_size: int = 65536  # 64KB 默认，可配置
    read_cursor: int = 0          # 读指针（已消费位置）
```

**数据流**：

```
远程串口 ──continuous──▶ com2tcp ──telnet──▶ TelnetSession.buffer
                                                    │
                          telnet_listen(duration)    │
                          ◄──────────────────────────┘
                          返回 buffer[read_cursor:] （新数据）
                          read_cursor 移到末尾

                          telnet_read_all()
                          返回 buffer[:] （全部数据）
                          read_cursor 移到末尾
```

| 工具 | 行为 | 移动 cursor |
|------|------|------------|
| `telnet_listen` | 监听 duration 秒，返回期间收到的新数据 | 是 |
| `telnet_read` | 读取缓冲中上次消费之后的新数据 | 是 |
| `telnet_read_all` | 返回缓冲区全部内容（含历史） | 是 |

### 4.3 二进制数据处理

串口数据可能包含非 UTF-8 字节。Telnet 缓冲区存储 **原始 bytes**，返回时提供两种编码选项：

- `encoding="utf-8"` (默认): 用 `errors="replace"` 解码为字符串
- `encoding="base64"`: 返回 base64 编码的原始字节
- `encoding="hex"`: 返回十六进制编码

### 4.4 自动重连

Telnet 同样支持自动重连。重连时重新执行完整的连接 + 登录流程。

---

## 5. com2tcp 工作流

### 5.1 场景

```
┌──────────────┐     SSH (PowerShell)     ┌─────────────────────────┐
│  MCP Server  │ ───────────────────────▶ │  Windows PC (目标机)     │
│              │                          │                          │
│              │  1. upload com2tcp.exe   │  C:\Users\Public\        │
│              │  2. 后台启动 com2tcp      │    com2tcp.exe            │
│              │                          │                          │
│              │     Telnet               │  COM4 ◄── 串口设备       │
│              │ ◄─────────────────────── │    :5200 (telnet)        │
└──────────────┘                          └─────────────────────────┘
```

### 5.2 执行步骤

```
1. 前置条件: SSH 已连接到 Windows PC (session_id)

2. 上传 com2tcp.exe
   ssh_upload_binary(session_id, "./com2tcp.exe",
                     "C:/Users/Public/com2tcp_{port}.exe")

3. 终止旧进程（如果存在）
   taskkill /F /IM com2tcp_{port}.exe >nul 2>&1

4. 后台启动
   Start-Process -WindowStyle Hidden -FilePath
     "C:/Users/Public/com2tcp_{port}.exe"
     -ArgumentList '--telnet --ignore-dsr --baud {baud} {com_port} {telnet_port}'

5. 验证进程存活
   Get-Process -Name "com2tcp_{port}" -ErrorAction SilentlyContinue

6. 返回结果
   {
     "status": "ok",
     "host": "<Windows PC IP>",
     "telnet_port": 5200,
     "com_port": "COM4",
     "next_step": "telnet_connect to host:5200"
   }
```

### 5.3 参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `baud` | 115200 | 串口波特率 |
| `--ignore-dsr` | 始终开启 | 忽略 DSR 信号 |
| `--telnet` | 始终开启 | Telnet 模式（非 RFC2217） |

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

### SSH (8 个)

| 工具 | 说明 | 关键参数 |
|------|------|---------|
| `ssh_connect` | 密码连接 | session_id, host, port, username, password |
| `ssh_connect_key` | 密钥连接 | session_id, host, port, username, key_file |
| `ssh_execute` | 执行命令 | session_id, command, timeout |
| `ssh_upload` | SCP 上传 | session_id, local_path, remote_path |
| `ssh_download` | SCP 下载 | session_id, remote_path, local_path |
| `ssh_upload_binary` | Base64 上传 | session_id, local_path, remote_path |
| `ssh_disconnect` | 关闭会话 | session_id |
| `ssh_list` | 列出会话 | — |

### Telnet (8 个)

| 工具 | 说明 | 关键参数 |
|------|------|---------|
| `telnet_connect` | 连接 | session_id, host, port, username?, password? |
| `telnet_execute` | 发送命令+读响应 | session_id, command, timeout |
| `telnet_send` | 发送原始数据 | session_id, data |
| `telnet_listen` | 监听新数据 | session_id, duration |
| `telnet_read` | 读取新数据（消费） | session_id, timeout |
| `telnet_read_all` | 读取全部缓冲（消费） | session_id |
| `telnet_disconnect` | 关闭会话 | session_id |
| `telnet_list` | 列出会话 | — |

### 工作流 (1 个)

| 工具 | 说明 |
|------|------|
| `setup_com2tcp` | SSH 上传 + 后台启动 com2tcp，返回 telnet 连接信息 |

### 通用 (1 个)

| 工具 | 说明 |
|------|------|
| `list_sessions` | 列出所有 SSH 和 Telnet 会话 |

**总计: 18 个工具**

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
- [ ] 配置文件支持（YAML/TOML 预设连接参数）
