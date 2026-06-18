import json

from ooptdd_loop import mcp_config
from ooptdd_loop.cli import main


def test_generates_codex_and_claude_registration_fragments(tmp_path):
    generated = mcp_config.generated_configs(
        cwd=str(tmp_path),
        pythonpath=str(tmp_path),
        oo_mcp_url="http://logserver.example/mcp",
    )

    codex = generated["codex"]
    assert "[mcp_servers.ooptdd-loop]" in codex["snippet"]
    assert 'command = "python"' in codex["snippet"]
    assert 'args = ["-m", "ooptdd_loop.mcp_server"]' in codex["snippet"]
    assert f'cwd = "{tmp_path}"' in codex["snippet"]
    assert "OO_MCP_URL" in codex["snippet"]
    assert "PYTHONPATH" in codex["snippet"]

    claude = generated["claude"]
    payload = json.loads(claude["snippet"])
    entry = payload["mcpServers"]["ooptdd-loop"]
    assert entry["command"] == "python"
    assert entry["args"] == ["-m", "ooptdd_loop.mcp_server"]
    assert entry["env"]["OO_MCP_URL"] == "http://logserver.example/mcp"
    assert "cwd" not in entry


def test_checks_temp_codex_and_claude_configs(tmp_path):
    generated = mcp_config.generated_configs(
        cwd=str(tmp_path),
        pythonpath=str(tmp_path),
        oo_mcp_url="http://logserver.example/mcp",
    )
    codex_path = tmp_path / "config.toml"
    codex_path.write_text(generated["codex"]["snippet"], encoding="utf-8")

    claude_path = tmp_path / "settings.json"
    claude_path.write_text(generated["claude"]["snippet"], encoding="utf-8")

    report = mcp_config.check_configs(
        generated=generated,
        paths={"codex": str(codex_path), "claude": str(claude_path)},
    )

    assert report["ok"] is True
    assert report["clients"]["codex"]["ok"] is True
    assert report["clients"]["claude"]["ok"] is True


def test_check_reports_mismatched_command(tmp_path):
    generated = mcp_config.generated_configs(
        cwd=str(tmp_path),
        pythonpath=str(tmp_path),
        oo_mcp_url="http://logserver.example/mcp",
    )
    codex_path = tmp_path / "config.toml"
    codex_path.write_text(
        generated["codex"]["snippet"].replace('command = "python"', 'command = "python3"'),
        encoding="utf-8",
    )

    report = mcp_config.check_configs(
        clients=("codex",),
        generated={"codex": generated["codex"]},
        paths={"codex": str(codex_path)},
    )

    assert report["ok"] is False
    command = [c for c in report["clients"]["codex"]["checks"] if c["field"] == "command"][0]
    assert command["ok"] is False
    assert command["current"] == "python3"


def test_cli_mcp_config_generates_json(capsys):
    assert main(["mcp-config", "--client", "codex", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["clients"]["codex"]["entry"]["args"] == ["-m", "ooptdd_loop.mcp_server"]


def test_cli_mcp_config_checks_temp_files(tmp_path, capsys):
    generated = mcp_config.generated_configs(
        cwd=str(tmp_path),
        pythonpath=str(tmp_path),
        oo_mcp_url="http://logserver.example/mcp",
    )
    codex_path = tmp_path / "config.toml"
    codex_path.write_text(generated["codex"]["snippet"], encoding="utf-8")

    assert main(
        [
            "mcp-config",
            "--client",
            "codex",
            "--check",
            "--json",
            "--cwd",
            str(tmp_path),
            "--pythonpath",
            str(tmp_path),
            "--oo-mcp-url",
            "http://logserver.example/mcp",
            "--codex-config",
            str(codex_path),
        ]
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
