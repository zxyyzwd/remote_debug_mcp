# Remote Debug MCP

基于 MCP (Model Context Protocol) 的远程调试服务器，支持 **SSH** 和 **Telnet** 后台持久化连接，专为远程串口调试和 Android/Linux 设备管理设计。

## 功能

- **SSH** — 持续后台连接远程服务器/Windows PC，支持命令执行、SCP/SFTP 文件传输、自动重连
- **Telnet** — 持续后台连接，支持命令执行、数据收发、串口数据监听
- **com2tcp** — 一键桥接 Windows COM 串口到 TCP Telnet，远程调试串口设备
- **配置文件** — YAML 文件预设连接参数，一键连接

## 安装

```bash
# 虚拟环境（推荐）
python3 -m venv .venv && source .venv/bin/activate && pip install -e .

# 或直接安装
pip install -e .
```

依赖：Python ≥ 3.10 · pexpect · mcp

## 配置客户端

### OpenCode

`~/.config/opencode/opencode.jsonc`：

```jsonc
{
  "mcp": {
    "remote-debug": {
      "type": "local",
      "command": ["python3", "-m", "remote_debug_mcp"],
      "enabled": true,
      "timeout": 60000
    }
  }
}
```

### Claude Code

`~/.claude.json`：

```json
{
  "mcp": {
    "remote-debug": {
      "command": "python3",
      "args": ["-m", "remote_debug_mcp"],
      "enabled": true,
      "timeout": 60000
    }
  }
}
```

也可用命令行添加：

```bash
claude mcp add remote-debug python3 -- -m remote_debug_mcp
```

> 使用虚拟环境时，将 `python3` 替换为 `.venv/bin/python3` 的绝对路径。

配置后重启客户端即可使用。

## 连接配置

支持两种方式提供 SSH / Telnet / COM 端口参数。

### 方式一：YAML 配置文件（推荐）

`config.example.yaml` 随包安装，位于包目录下。复制为 `config.yaml` 即可使用：

```bash
# 找到示例文件
python3 -c "from remote_debug_mcp.config_loader import example_config_path; print(example_config_path())"

# 复制到当前目录或全局配置目录
cp <example_path> ./config.yaml
# 或
mkdir -p ~/.config/remote-debug-mcp
cp <example_path> ~/.config/remote-debug-mcp/config.yaml
```

`config.yaml` 默认搜索路径：
- `./config.yaml`（当前工作目录）
- `<package_dir>/config.yaml`（包安装目录）
- `~/.config/remote-debug-mcp/config.yaml`（用户全局配置）

填写以下参数：

```yaml
connections:
  # ── SSH 连接 ──────────────────────
  - name: "office-pc"          # 连接名称，后续引用用
    type: ssh
    host: "192.168.1.16"       # 远程 IP
    port: 22                   # SSH 端口
    username: "zxyyz"          # 用户名
    password: "kaixin123"      # 密码（明文，仅本地使用）

  - name: "office-pc-key"      # 密钥认证
    type: ssh
    host: "192.168.1.16"
    username: "admin"
    key_file: "/home/me/.ssh/id_rsa"

  # ── 串口映射 ──────────────────────
  - name: "serial-com4"        # com2tcp 桥接配置
    type: com2tcp
    ssh: "office-pc"           # 引用上面的 SSH 连接名
    com_port: "COM4"           # Windows COM 口
    telnet_port: 5200          # 暴露的 Telnet 端口
    baud: 115200               # 波特率（默认 115200）
```

使用：

```
load_config → path: "config.yaml"           # 加载配置
connect_from_config → config_name: "office-pc"  # 用名称连接
setup_com2tcp_from_config → config_name: "serial-com4"  # 串口桥接
```

### 方式二：MCP 工具直接传参

```
ssh_connect → session_id: "my-session", host: "192.168.1.16",
              username: "admin", password: "xxx"

# 串口桥接需要先 SSH 连接，再调用
ssh_connect → session_id: "win", host: "192.168.1.16", ...
setup_com2tcp → ssh_session_id: "win", com_port: "COM4",
                telnet_port: 5200, baud: 115200
```

## com2tcp 串口调试工作流

```
┌──────────┐  SSH (PowerShell)   ┌──────────────────┐
│  MCP     │ ──────────────────▶ │  Windows PC       │
│  Server  │                     │  COM4 → :5200     │
│          │  Telnet :5200       │       ↑           │
│          │ ◀────────────────── │  串口设备          │
└──────────┘                     └──────────────────┘
```

```
1. load_config → path: "config.yaml"
2. setup_com2tcp_from_config → config_name: "serial-com4"
3. telnet_connect → host: "192.168.1.16", port: 5200
4. telnet_execute → command: "ls"
5. telnet_listen → duration: 10
```

## 工具参考

### SSH（8 个）

| 工具 | 说明 |
|------|------|
| `ssh_connect` | 密码连接，后台持久化 |
| `ssh_connect_key` | 私钥连接 |
| `ssh_execute` | 执行命令（自动适配 bash/PowerShell） |
| `ssh_upload` | SCP 上传（优先 SCP → SFTP → Base64） |
| `ssh_download` | SCP 下载（优先 SCP → SFTP → SSH Base64） |
| `ssh_upload_binary` | Base64 上传（通用，Windows 兼容） |
| `ssh_disconnect` | 关闭会话 |
| `ssh_list` | 列出所有 SSH 会话 |

### Telnet（8 个）

| 工具 | 说明 |
|------|------|
| `telnet_connect` | 连接（可选用户名/密码，可配缓冲区） |
| `telnet_execute` | 发送命令并等待响应 |
| `telnet_send` | 发送原始数据（不等待） |
| `telnet_listen` | 监听指定秒数，返回新数据 |
| `telnet_read` | 读取缓冲区新数据 |
| `telnet_read_all` | 读取并清空全部缓冲区 |
| `telnet_disconnect` | 关闭会话 |
| `telnet_list` | 列出所有 Telnet 会话 |

### 配置（4 个）

| 工具 | 说明 |
|------|------|
| `load_config` | 加载 YAML 配置文件 |
| `list_connections` | 列出配置中的所有连接 |
| `connect_from_config` | 按名称一键 SSH 连接 |
| `setup_com2tcp_from_config` | 按名称一键 com2tcp 部署 |

### 通用（2 个）

| 工具 | 说明 |
|------|------|
| `setup_com2tcp` | 手动 com2tcp 工作流 |
| `list_sessions` | 列出所有 SSH + Telnet 会话 |

## 架构

```
src/remote_debug_mcp/
├── server.py         # MCP 服务端：22 个工具定义 + 分发
├── sessions.py       # SSH/Telnet 会话生命周期管理
├── config_loader.py  # YAML 配置文件加载
├── com2tcp.exe       # com2tcp 桥接工具（随包发布）
├── __init__.py
└── __main__.py
```

## 技术要点

- SSH 使用 `pexpect.spawn('ssh', ...)` 直连，**不使用 pxssh**（避免 Windows 提示符兼容问题）
- 命令输出通过 `echo` 唯一标记分隔，不依赖 shell 提示符
- Windows 自动切换到 PowerShell，工作目录 `D:\remote_debug`
- Telnet 缓冲区 64KB（可配），FIFO 滚动淘汰，支持 utf-8/base64/hex 编码
- 自动重连：指数退避，默认最多 3 次

详细设计参见 [DESIGN.md](DESIGN.md)
