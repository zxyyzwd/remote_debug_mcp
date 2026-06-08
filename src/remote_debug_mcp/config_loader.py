"""
配置文件加载器，读取 config.yaml 中的连接参数。
支持 SSH 连接和 com2tcp 串口映射配置。
"""
import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SSHConfig:
    name: str
    host: str
    port: int = 22
    username: str = ""
    password: str = ""
    key_file: str = ""


@dataclass
class Com2TcpConfig:
    name: str
    ssh: str
    com_port: str
    telnet_port: int = 5200
    baud: int = 115200


@dataclass
class AppConfig:
    ssh_connections: list[SSHConfig] = field(default_factory=list)
    com2tcp_connections: list[Com2TcpConfig] = field(default_factory=list)

    def get_ssh(self, name: str) -> Optional[SSHConfig]:
        for c in self.ssh_connections:
            if c.name == name:
                return c
        return None

    def get_com2tcp(self, name: str) -> Optional[Com2TcpConfig]:
        for c in self.com2tcp_connections:
            if c.name == name:
                return c
        return None


def _parse_yaml_simple(content: str) -> AppConfig:
    """简版 YAML 解析器，无需 PyYAML 依赖。"""
    config = AppConfig()
    current_section = None
    current_entry = {}
    in_connections = False

    for raw_line in content.split("\n"):
        line = raw_line.rstrip()
        if not line or line.strip().startswith("#"):
            continue

        stripped = line.strip()

        if stripped == "connections:":
            in_connections = True
            continue

        if not in_connections:
            continue

        if stripped.startswith("- name:"):
            if current_entry:
                _add_entry(config, current_entry)
                current_entry = {}
            name_part = stripped.split(":", 1)[1].strip()
            if "#" in name_part:
                name_part = name_part[:name_part.index("#")].strip()
            current_entry["name"] = name_part.strip().strip('"')

        elif ":" in stripped and not stripped.startswith("-"):
            key, _, val = stripped.partition(":")
            key = key.strip()
            val = val.strip()
            if "#" in val:
                val = val[:val.index("#")].strip()
            val = val.strip('"').strip("'")
            if key in ("port", "telnet_port", "baud"):
                current_entry[key] = int(val) if val else 0
            else:
                current_entry[key] = val

    if current_entry:
        _add_entry(config, current_entry)

    return config


def _add_entry(config: AppConfig, entry: dict):
    name = entry.get("name", "")
    etype = entry.get("type", "ssh")
    if etype == "ssh":
        config.ssh_connections.append(SSHConfig(
            name=name,
            host=entry.get("host", ""),
            port=entry.get("port", 22),
            username=entry.get("username", ""),
            password=entry.get("password", ""),
            key_file=entry.get("key_file", ""),
        ))
    elif etype == "com2tcp":
        config.com2tcp_connections.append(Com2TcpConfig(
            name=name,
            ssh=entry.get("ssh", ""),
            com_port=entry.get("com_port", ""),
            telnet_port=entry.get("telnet_port", 5200),
            baud=entry.get("baud", 115200),
        ))


def load_config(path: str = "config.yaml") -> AppConfig:
    """加载配置文件，自动搜索多个路径。"""
    source_dir = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.join(source_dir, "..", "..", "..")
    search_paths = [
        path,
        os.path.join(source_dir, path),
        os.path.join(source_dir, "..", path),
        os.path.join(repo_root, path),
        os.path.join(repo_root, "src", "remote_debug_mcp", path),
        os.path.join(os.path.expanduser("~"), ".config", "remote-debug-mcp", path),
    ]
    for p in search_paths:
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                return _parse_yaml_simple(f.read())
    raise FileNotFoundError(f"Config file not found: {path} (searched: {search_paths})")


def example_config_path() -> str:
    """返回 config.example.yaml 的路径（用于参考/复制）。"""
    source_dir = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.join(source_dir, "..", "..", "..")
    search = [
        os.path.join(source_dir, "config.example.yaml"),
        os.path.join(repo_root, "config.example.yaml"),
    ]
    for p in search:
        if os.path.exists(p):
            return p
    return "config.example.yaml (not found, check repo)"


_config: Optional[AppConfig] = None


def get_config(path: str = "config.yaml") -> AppConfig:
    global _config
    if _config is None:
        _config = load_config(path)
    return _config


def reload_config(path: str = "config.yaml") -> AppConfig:
    global _config
    _config = load_config(path)
    return _config
