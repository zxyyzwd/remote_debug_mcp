# Remote Debug MCP

基于 MCP (Model Context Protocol) 的远程调试服务器，支持 **SSH** 和 **Telnet** 后台持久化连接，专为远程串口调试和 Android/Linux 设备管理设计。

## 功能

- **SSH** — 持续后台连接远程服务器/Windows PC，支持命令执行、SCP/SFTP 文件传输、MD5 完整性校验、自动重连
- **Telnet** — 持续后台连接，支持数据收发、串口数据监听、后台监控保存
- **com2tcp** — 一键桥接 Windows COM 串口到 TCP Telnet，远程调试串口设备
- **配置文件** — YAML 文件预设连接参数，`ssh_connect(config_name="xxx")` 一键连接

## 安装

```bash
# 克隆到 ~/.config 目录（推荐，与配置文件路径一致）
mkdir -p ~/.config
git clone https://github.com/zxyyzwd/remote_debug_mcp.git ~/.config/remote_debug_mcp
cd ~/.config/remote_debug_mcp

# 虚拟环境安装
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

> 使用虚拟环境时，将 `python3` 替换为虚拟机环境中的 Python 路径。

配置后重启客户端即可使用。

## 连接配置

### YAML 配置文件

`config.yaml` 放在工作目录或仓库根目录，首次调用 `ssh_connect(config_name=...)` 时自动加载并缓存。

`config.yaml` 默认搜索路径：
- `./config.yaml`（当前工作目录）
- `<source_dir>/config.yaml`（源码目录）
- `<source_dir>/../config.yaml`（`src/` 父目录）
- `<repo_root>/config.yaml`（仓库根目录）
- `<repo_root>/src/remote_debug_mcp/config.yaml`（源码包目录）
- `~/.config/remote-debug-mcp/config.yaml`（用户全局配置）

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
    ssh: "office-pc"           # 引用上面的 SSH 连接名（用于解析 host）
    com_port: "COM4"           # Windows COM 口
    telnet_port: 5200          # 暴露的 Telnet 端口
    baud: 115200               # 波特率（默认 115200）
    # 以下为可选参数（均有默认值）
    # username: ""             # Telnet 登录用户名
    # password: ""             # Telnet 登录密码
    # connect_timeout: 15      # 连接超时（秒）
    # buffer_max_size: 65536   # 缓冲区大小（字节，默认 64KB）
    # max_retries: 3           # 自动重连次数
```

使用：

```
ssh_connect → config_name: "office-pc"            # 自动从 config.yaml 读参数连接
list_connections                                   # 查看已加载的配置
save_config                                        # 保存当前内存配置
save_config → connections: [{...}]                 # 创建/更新配置条目（无配置文件时的唯一入库入口）
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
1. ssh_connect → config_name: "office-pc"    # SSH 到 Windows PC
2. setup_com2tcp → ssh_session_id: "...", com_port: "COM4", telnet_port: 5200
3. save_config → connections: [{name: "serial-com4", type: "com2tcp", ...}]  # 持久化配置
4. telnet_connect → session_id: "serial", config_name: "serial-com4"         # 所有参数从配置解析
5. telnet_send → data: "ls", timeout: 3
6. telnet_listen → duration: 10
7. telnet_start_monitor → output_file: "serial.log"  # 后台持续记录
```

## 工具参考

### SSH（6 个）

| 工具 | 说明 |
|------|------|
| `ssh_connect` | 通过 config_name 从 config.yaml 读取参数连接（密码/密钥自适应） |
| `ssh_execute` | 执行命令（自动适配 bash/PowerShell，中文编码正确） |
| `ssh_upload` | 上传文件（SCP → SFTP 降级，自动 MD5 校验） |
| `ssh_download` | 下载文件（SCP → SFTP 降级，自动 MD5 校验） |
| `ssh_disconnect` | 关闭会话 |
| `ssh_list` | 列出所有 SSH 会话 |

### Telnet（5 个）

| 工具 | 说明 |
|------|------|
| `telnet_connect` | 通过 config_name 连接（host/port/login/buffer/retries 全从配置解析） |
| `telnet_send` | 发送数据（timeout=0 发后即返，timeout>0 等响应；`__CTRL_C__`/`__CTRL_D__`/`__CTRL_Z__`） |
| `telnet_listen` | 监听指定秒数，返回新数据（支持 utf-8/base64/hex 编码） |
| `telnet_disconnect` | 关闭会话 |
| `telnet_list` | 列出所有 Telnet 会话 |

### Telnet 监控（2 个）

| 工具 | 说明 |
|------|------|
| `telnet_start_monitor` | 启动后台持续监听（可选持续写入文件） |
| `telnet_stop_monitor` | 停止后台监听，返回累计行数 |

### 配置（2 个）

| 工具 | 说明 |
|------|------|
| `list_connections` | 列出已加载配置中的所有 SSH 和 com2tcp 连接 |
| `save_config` | **配置唯一入库入口**：无参保存内存配置；带 `connections` 参数创建/更新条目后写入文件 |

### 工作流（1 个）

| 工具 | 说明 |
|------|------|
| `setup_com2tcp` | 完整 com2tcp 工作流（上传 + 启动 + 验证），完成后提示调 `save_config` 持久化 |

### 通用（1 个）

| 工具 | 说明 |
|------|------|
| `list_sessions` | 列出所有 SSH + Telnet 会话 |

## 架构

```
src/remote_debug_mcp/
├── server.py         # MCP 服务端：17 个工具定义 + 分发
├── sessions.py       # SSH/Telnet 会话生命周期管理
├── config_loader.py  # YAML 配置文件加载/保存
├── com2tcp.exe       # com2tcp 桥接工具（随包发布）
├── config.example.yaml
├── __init__.py
└── __main__.py
```

## 技术要点

- SSH 使用 `pexpect.spawn('ssh', ...)` 直连，**不使用 pxssh**（避免 Windows 提示符兼容问题）
- pexpect `encoding=None` 原始字节模式，应用层按平台分编码（Windows: GBK, Linux: UTF-8）
- 命令输出通过 `echo` 唯一标记分隔，不依赖 shell 提示符
- Windows 自动切换到 PowerShell，工作目录 `D:\remote_debug`
- 文件传输 SCP 优先 → SFTP 兜底，传输后自动 MD5 校验
- Telnet `telnet_send` 合并原 `telnet_execute`，`timeout=0` 不等待，`timeout>0` 等响应
- Telnet 缓冲区 64KB（可配），支持 utf-8/base64/hex 编码
- Telnet 后台监听：deque 行缓存 90 万行（FIFO），可选持续写入文件
- 自动重连：指数退避，默认最多 3 次
- 配置自动缓存内存，`save_config` 持久化到 YAML

详细设计参见 [DESIGN.md](DESIGN.md)
