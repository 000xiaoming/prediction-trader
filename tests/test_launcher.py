"""Exercise the public launcher without starting Docker or Windows processes."""

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture
def launcher(tmp_path: Path) -> tuple[Path, dict[str, str], Path]:
    root = tmp_path / "checkout with spaces"
    root.mkdir()
    source = Path(__file__).resolve().parents[1]
    shutil.copyfile(source / "pt", root / "pt")
    shutil.copyfile(source / ".env.example", root / ".env.example")
    commands = tmp_path / "bin"
    commands.mkdir()
    log = tmp_path / "calls.jsonl"
    fake = f"#!{sys.executable}\n" + '''import json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
with open(os.environ["CALL_LOG"], "a") as output:
    output.write(json.dumps([name, *sys.argv[1:]]) + "\\n")
if name == "wslpath":
    print(sys.argv[-1])
if name == "docker" and "config" in sys.argv and os.environ.get("FAIL_CONFIG"):
    sys.exit(2)
'''
    for name in ("docker", "powershell.exe", "wslpath"):
        path = commands / name
        path.write_text(fake)
        path.chmod(0o755)
    environment = {
        "PATH": f"{commands}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "CALL_LOG": str(log),
    }
    return root, environment, log


def _run(
    launcher: tuple[Path, dict[str, str], Path], *args: str
) -> subprocess.CompletedProcess[str]:
    root, environment, _ = launcher
    return subprocess.run(
        ["bash", str(root / "pt"), *args],
        cwd=root.parent,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def _calls(log: Path) -> list[list[str]]:
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def test_init_is_private_idempotent_and_does_not_require_docker(
    launcher: tuple[Path, dict[str, str], Path],
) -> None:
    root, _, log = launcher
    assert _run(launcher, "init").returncode == 0
    dotenv = root / ".env"
    assert stat.S_IMODE(dotenv.stat().st_mode) == 0o600
    dotenv.write_text("LOCAL_SETTING=preserve-me\n")
    result = _run(launcher, "init")
    assert result.returncode == 0
    assert dotenv.read_text() == "LOCAL_SETTING=preserve-me\n"
    assert not _calls(log)


def test_help_and_missing_env_give_actionable_results(
    launcher: tuple[Path, dict[str, str], Path],
) -> None:
    _, _, log = launcher
    result = _run(launcher, "--help")
    assert result.returncode == 0 and "portable|wsl-relay" in result.stdout
    result = _run(launcher, "start")
    assert result.returncode != 0 and "./pt init" in result.stderr
    assert not _calls(log)


def test_default_start_validates_then_uses_only_portable_compose(
    launcher: tuple[Path, dict[str, str], Path],
) -> None:
    root, _, log = launcher
    assert _run(launcher, "init").returncode == 0
    result = _run(launcher, "start", "--wait")
    assert result.returncode == 0, result.stderr
    calls = _calls(log)
    assert all(call[0] == "docker" for call in calls)
    assert "config" in calls[-2] and "--quiet" in calls[-2]
    assert calls[-1][-4:] == ["up", "-d", "--build", "--wait"]
    assert calls[-1].count("-f") == 1
    assert str(root / ".env") in calls[-1]
    assert not (root / "data").exists()


def test_invalid_configuration_prevents_start(
    launcher: tuple[Path, dict[str, str], Path],
) -> None:
    _, environment, log = launcher
    assert _run(launcher, "init").returncode == 0
    environment["FAIL_CONFIG"] = "1"
    assert _run(launcher, "start").returncode != 0
    assert not any("up" in call for call in _calls(log))


def test_workstation_mode_is_explicit_and_keeps_research_overlay(
    launcher: tuple[Path, dict[str, str], Path],
) -> None:
    root, environment, log = launcher
    assert _run(launcher, "init").returncode == 0
    (root / ".pt-mode").write_text("wsl-relay\n")
    environment["PT_WINDOWS_PYTHON"] = "C:\\Python 3\\python.exe"
    result = _run(launcher, "start")
    assert result.returncode == 0, result.stderr
    calls = _calls(log)
    relay = next(call for call in calls if call[0] == "powershell.exe")
    assert relay[-2:] == ["-Python", "C:\\Python 3\\python.exe"]
    assert calls[-1].count("-f") == 3
    assert (root / "data" / "research").is_dir()


def test_command_line_mode_takes_precedence_over_environment_and_local_file(
    launcher: tuple[Path, dict[str, str], Path],
) -> None:
    root, environment, log = launcher
    assert _run(launcher, "init").returncode == 0
    (root / ".pt-mode").write_text("wsl-relay\n")
    environment["PT_DEPLOYMENT_MODE"] = "wsl-relay"
    result = _run(launcher, "--mode", "portable", "check")
    assert result.returncode == 0, result.stderr
    assert _calls(log)[-1].count("-f") == 1
    assert "portable" in result.stdout


def test_unknown_mode_fails_before_running_commands(
    launcher: tuple[Path, dict[str, str], Path],
) -> None:
    _, _, log = launcher
    result = _run(launcher, "--mode", "typo", "start")
    assert result.returncode == 2
    assert "Unknown deployment mode" in result.stderr
    assert not _calls(log)
