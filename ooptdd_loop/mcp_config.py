"""Generate and verify Claude/Codex MCP registration snippets."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .log_mcp import resolve_mcp_url


SERVER_NAME = "ooptdd-loop"
SERVER_MODULE = "ooptdd_loop.mcp_server"
DESCRIPTION = "OOPTDD loop MCP server with upstream oo-mcp logserver bridge"
CLIENTS = ("codex", "claude")


def default_project_root(start: str | os.PathLike | None = None) -> str:
    """Find the nearest ooptdd-loop checkout, falling back to the current directory."""
    current = Path(start or os.getcwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / "pyproject.toml").exists() and (candidate / "ooptdd_loop").is_dir():
            return str(candidate)
    return str(current)


def default_config_path(client: str) -> str:
    home = Path.home()
    if client == "codex":
        return str(home / ".codex" / "config.toml")
    if client == "claude":
        return str(home / ".claude" / "settings.json")
    raise ValueError(f"unknown MCP client {client!r}")


def server_entry(
    *,
    client: str,
    server_name: str = SERVER_NAME,
    command: str = "python",
    module: str = SERVER_MODULE,
    cwd: str | None = None,
    pythonpath: str | None = None,
    oo_mcp_url: str | None = None,
    include_cwd_for_claude: bool = False,
) -> dict[str, Any]:
    """Return the MCP server object for a client config."""
    if client not in CLIENTS:
        raise ValueError(f"unknown MCP client {client!r}")
    env: dict[str, str] = {}
    if oo_mcp_url:
        env["OO_MCP_URL"] = oo_mcp_url
    if pythonpath:
        env["PYTHONPATH"] = pythonpath

    entry: dict[str, Any] = {
        "command": command,
        "args": ["-m", module],
    }
    if cwd and (client == "codex" or include_cwd_for_claude):
        entry["cwd"] = cwd
    if env:
        entry["env"] = env
    if client == "claude":
        entry["_description"] = DESCRIPTION
    return entry


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _toml_array(values: list[str]) -> str:
    return "[" + ", ".join(_toml_string(v) for v in values) + "]"


def _toml_inline_table(values: dict[str, str]) -> str:
    return "{ " + ", ".join(f"{k} = {_toml_string(v)}" for k, v in values.items()) + " }"


def codex_toml_snippet(server_name: str, entry: dict[str, Any]) -> str:
    lines = [f"[mcp_servers.{server_name}]"]
    lines.append(f"command = {_toml_string(str(entry['command']))}")
    lines.append(f"args = {_toml_array([str(v) for v in entry['args']])}")
    if entry.get("cwd"):
        lines.append(f"cwd = {_toml_string(str(entry['cwd']))}")
    if entry.get("env"):
        lines.append(f"env = {_toml_inline_table(entry['env'])}")
    return "\n".join(lines) + "\n"


def claude_json_snippet(server_name: str, entry: dict[str, Any]) -> str:
    return json.dumps({"mcpServers": {server_name: entry}}, ensure_ascii=False, indent=2) + "\n"


def generated_configs(
    *,
    clients: list[str] | tuple[str, ...] = CLIENTS,
    server_name: str = SERVER_NAME,
    command: str = "python",
    module: str = SERVER_MODULE,
    cwd: str | None = None,
    pythonpath: str | None = None,
    oo_mcp_url: str | None = None,
    include_cwd_for_claude: bool = False,
) -> dict[str, dict[str, Any]]:
    cwd = cwd or default_project_root()
    pythonpath = pythonpath if pythonpath is not None else cwd
    oo_mcp_url = oo_mcp_url if oo_mcp_url is not None else resolve_mcp_url()
    out: dict[str, dict[str, Any]] = {}
    for client in clients:
        entry = server_entry(
            client=client,
            server_name=server_name,
            command=command,
            module=module,
            cwd=cwd,
            pythonpath=pythonpath,
            oo_mcp_url=oo_mcp_url,
            include_cwd_for_claude=include_cwd_for_claude,
        )
        snippet = (
            codex_toml_snippet(server_name, entry)
            if client == "codex"
            else claude_json_snippet(server_name, entry)
        )
        out[client] = {
            "client": client,
            "config_path": default_config_path(client),
            "server_name": server_name,
            "entry": entry,
            "snippet": snippet,
        }
    return out


def _load_codex_config(path: str) -> tuple[dict[str, Any] | None, str | None]:
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - Python 3.10 fallback
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ModuleNotFoundError:
            return None, "tomllib/tomli is required to parse Codex TOML config"

    try:
        return tomllib.loads(Path(path).read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, "config file does not exist"
    except Exception as exc:
        return None, f"could not parse Codex config: {exc}"


def _load_claude_config(path: str) -> tuple[dict[str, Any] | None, str | None]:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, "config file does not exist"
    except Exception as exc:
        return None, f"could not parse Claude config: {exc}"


def _redact(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    if "://" in value and "@" in value:
        scheme, rest = value.split("://", 1)
        return scheme + "://***@" + rest.rsplit("@", 1)[-1]
    return value


def _compare_field(checks: list[dict[str, Any]], field: str, current: Any, expected: Any) -> None:
    checks.append(
        {
            "field": field,
            "ok": current == expected,
            "current": _redact(current),
            "expected": _redact(expected),
        }
    )


def check_config(
    *,
    client: str,
    path: str | None = None,
    expected: dict[str, Any],
    server_name: str = SERVER_NAME,
    require_cwd: bool = False,
) -> dict[str, Any]:
    if client == "codex":
        loaded, error = _load_codex_config(path or default_config_path(client))
        servers = (loaded or {}).get("mcp_servers", {}) if loaded else {}
    elif client == "claude":
        loaded, error = _load_claude_config(path or default_config_path(client))
        servers = (loaded or {}).get("mcpServers", {}) if loaded else {}
    else:
        raise ValueError(f"unknown MCP client {client!r}")

    result: dict[str, Any] = {
        "client": client,
        "config_path": path or default_config_path(client),
        "server_name": server_name,
        "ok": False,
        "checks": [],
    }
    if error:
        result["error"] = error
        return result

    current = servers.get(server_name) if isinstance(servers, dict) else None
    result["present"] = isinstance(current, dict)
    if not isinstance(current, dict):
        result["error"] = "server is not registered"
        return result

    checks: list[dict[str, Any]] = []
    _compare_field(checks, "command", current.get("command"), expected.get("command"))
    _compare_field(checks, "args", current.get("args"), expected.get("args"))
    if require_cwd and expected.get("cwd"):
        _compare_field(checks, "cwd", current.get("cwd"), expected.get("cwd"))
    for key, expected_value in expected.get("env", {}).items():
        _compare_field(checks, f"env.{key}", current.get("env", {}).get(key), expected_value)
    result["checks"] = checks
    result["ok"] = all(c["ok"] for c in checks)
    return result


def check_configs(
    *,
    clients: list[str] | tuple[str, ...] = CLIENTS,
    generated: dict[str, dict[str, Any]],
    paths: dict[str, str | None] | None = None,
    require_cwd: bool = False,
) -> dict[str, Any]:
    paths = paths or {}
    checks = {
        client: check_config(
            client=client,
            path=paths.get(client),
            expected=generated[client]["entry"],
            server_name=generated[client]["server_name"],
            require_cwd=require_cwd or client == "codex",
        )
        for client in clients
    }
    return {"ok": all(item["ok"] for item in checks.values()), "clients": checks}
