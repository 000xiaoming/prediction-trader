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
    print("private-config-value", file=sys.stderr)
    sys.exit(2)
if name == "docker" and "info" in sys.argv and os.environ.get("FAIL_DAEMON"):
    sys.exit(1)
if name == "docker" and "ps" in sys.argv:
    print(os.environ.get("CONTAINER_STATES", ""))
if name == "docker" and "exec" in sys.argv:
    if any("exec pg_dump " in arg for arg in sys.argv):
        sys.stdout.buffer.write(b"PGDMP-complete-test-archive")
        sys.exit(1 if os.environ.get("FAIL_DUMP") else 0)
    if "pg_restore" in sys.argv:
        content = sys.stdin.buffer.read()
        sys.exit(1 if os.environ.get("FAIL_ARCHIVE") or not content.startswith(b"PGDMP") else 0)
    if "sha256sum" in sys.argv:
        import hashlib
        content = sys.stdin.buffer.read()
        digest = hashlib.sha256(content).hexdigest()+"  -"
        print("invalid" if os.environ.get("FAIL_CHECKSUM") else digest)

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


def test_doctor_reports_stopped_missing_and_unhealthy_services_without_mutation(
    launcher: tuple[Path, dict[str, str], Path],
) -> None:
    _, environment, log = launcher
    assert _run(launcher, "init").returncode == 0
    environment["CONTAINER_STATES"] = (
        "trader|running|unhealthy\npostgres|exited|\nprometheus|running|"
    )
    result = _run(launcher, "doctor")
    assert result.returncode == 1
    for message in ("health=unhealthy", "postgres: exited", "grafana: container missing"):
        assert message in result.stdout
    calls = _calls(log)
    assert all(call[0] == "docker" for call in calls)
    assert not any(action in call for call in calls for action in ("up", "run", "stop", "exec"))
    assert "--all" in calls[-1] and "--orphans=false" in calls[-1]


def test_doctor_success_and_healthcheck_requirements(
    launcher: tuple[Path, dict[str, str], Path],
) -> None:
    _, environment, _ = launcher
    assert _run(launcher, "init").returncode == 0
    environment["CONTAINER_STATES"] = (
        "trader|running|healthy\npostgres|running|healthy\n"
        "prometheus|running|\ngrafana|running|"
    )
    assert _run(launcher, "doctor").returncode == 0
    environment["CONTAINER_STATES"] = environment["CONTAINER_STATES"].replace(
        "trader|running|healthy", "trader|running|",
    )
    result = _run(launcher, "doctor")
    assert result.returncode == 1 and "health check missing" in result.stdout


def test_doctor_configuration_and_daemon_failures_are_actionable_and_redacted(
    launcher: tuple[Path, dict[str, str], Path],
) -> None:
    _, environment, log = launcher
    result = _run(launcher, "doctor")
    assert result.returncode == 1 and "./pt init" in result.stdout
    assert not _calls(log)
    assert _run(launcher, "init").returncode == 0
    environment["FAIL_CONFIG"] = "1"
    result = _run(launcher, "doctor")
    assert result.returncode == 1 and "./pt check" in result.stdout
    assert "private-config-value" not in result.stdout + result.stderr
    del environment["FAIL_CONFIG"]
    environment["FAIL_DAEMON"] = "1"
    result = _run(launcher, "doctor")
    assert result.returncode == 1 and "Docker daemon" in result.stdout


@pytest.mark.parametrize("arguments", [
    ["backup", "/data/backups/a.db"], ["status", "--limit", "5"],
    ["list"], ["account", "paper:binance_btc_spot"], ["history", "alpha", "--limit", "5"],
    ["export", "alpha", "--format", "jsonl"], ["check"], ["activity", "alpha"],
])
@pytest.mark.parametrize("mode", ["portable", "wsl-relay"])
def test_ledger_maintenance_uses_a_one_off_container_without_starting_dependencies_or_relay(
    launcher: tuple[Path, dict[str, str], Path], arguments: list[str], mode: str,
) -> None:
    root, _, log = launcher
    assert _run(launcher, "init").returncode == 0
    result = _run(launcher, "--mode", mode, "ledger", *arguments)
    assert result.returncode == 0, result.stderr
    calls = _calls(log)
    assert all(call[0] == "docker" for call in calls)
    assert calls[-1][-(7 + len(arguments)):] == [
        "run", "--rm", "--no-deps", "-T", "--entrypoint", "prediction-paper", "trader",
        *arguments,
    ]
    assert not (root / "data").exists()


@pytest.mark.parametrize("command", ["paper-run", "preview", "quote", "mark", "funding", "settle"])
def test_ledger_rejects_order_commands_and_invalid_config(
    launcher: tuple[Path, dict[str, str], Path], command: str,
) -> None:
    _, environment, log = launcher
    assert _run(launcher, "ledger", command).returncode == 2
    assert not _calls(log)
    assert _run(launcher, "init").returncode == 0
    environment["FAIL_CONFIG"] = "1"
    assert _run(launcher, "ledger", "restore", "/data/backup").returncode != 0
    assert not any("run" in call for call in _calls(log))


def test_build_updates_the_image_without_starting_services(
    launcher: tuple[Path, dict[str, str], Path],
) -> None:
    _, _, log = launcher
    assert _run(launcher, "init").returncode == 0
    assert _run(launcher, "build", "trader").returncode == 0
    calls = _calls(log)
    assert calls[-1][-2:] == ["build", "trader"]
    assert not any(action in call for call in calls for action in ("up", "run", "exec"))



def test_postgres_backup_publishes_a_private_archive_without_starting_services(
    launcher: tuple[Path, dict[str, str], Path],
) -> None:
    import hashlib

    root, _, log = launcher
    assert _run(launcher, "init").returncode == 0
    target = root / "backups with spaces" / "database.dump"
    result = _run(launcher, "pg-backup", str(target))
    assert result.returncode == 0, result.stderr
    assert target.read_bytes() == b"PGDMP-complete-test-archive"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert hashlib.sha256(target.read_bytes()).hexdigest() in result.stdout
    assert not list(target.parent.glob(".postgres-backup.*"))
    calls = _calls(log)
    assert not any(action in call for call in calls for action in ("up", "run", "stop"))
    assert all(call[0] == "docker" for call in calls)
    assert any("--file=/dev/null" in call for call in calls)


@pytest.mark.parametrize("failure", ["FAIL_CONFIG", "FAIL_DUMP", "FAIL_ARCHIVE", "FAIL_CHECKSUM"])
def test_failed_postgres_backup_never_publishes_partial_output(
    launcher: tuple[Path, dict[str, str], Path], failure: str,
) -> None:
    root, environment, _ = launcher
    assert _run(launcher, "init").returncode == 0
    environment[failure] = "1"
    target = root / "backups" / "failed.dump"
    assert _run(launcher, "pg-backup", str(target)).returncode != 0
    assert not target.exists() and not list(root.glob("backups/.postgres-backup.*"))


@pytest.mark.parametrize("kind", ["file", "directory", "symlink"])
def test_postgres_backup_refuses_existing_destinations(
    launcher: tuple[Path, dict[str, str], Path], kind: str,
) -> None:
    root, _, log = launcher
    assert _run(launcher, "init").returncode == 0
    target = root / "existing.dump"
    if kind == "directory":
        target.mkdir()
    elif kind == "symlink":
        target.symlink_to(root / "missing.dump")
    else:
        target.write_text("original")
    assert _run(launcher, "pg-backup", str(target)).returncode != 0
    assert not any("exec" in call for call in _calls(log))
    if kind == "file":
        assert target.read_text() == "original"
    elif kind == "directory":
        assert not list(target.iterdir())
    else:
        assert target.is_symlink()



@pytest.mark.parametrize("kind", ["file", "directory"])
def test_postgres_backup_cannot_replace_a_destination_created_during_publication(
    launcher: tuple[Path, dict[str, str], Path], kind: str,
) -> None:
    root, environment, _ = launcher
    assert _run(launcher, "init").returncode == 0
    real_link = shutil.which("ln")
    assert real_link
    shim = Path(environment["PATH"].split(os.pathsep)[0]) / "ln"
    shim.write_text(f"#!{sys.executable}\n" + f"""import os, pathlib, sys
path = pathlib.Path(sys.argv[-1])
if {kind!r} == 'directory':
    path.mkdir()
else:
    path.write_text('another backup')
os.execv({real_link!r}, [{real_link!r}, *sys.argv[1:]])
""")
    shim.chmod(0o755)
    target = root / "raced.dump"
    assert _run(launcher, "pg-backup", str(target)).returncode != 0
    if kind == "directory":
        assert not list(target.iterdir())
    else:
        assert target.read_text() == "another backup"
    assert not list(root.glob(".postgres-backup.*"))


@pytest.mark.parametrize("arguments", [[], [""], ["a.dump", "b.dump"]])
def test_postgres_backup_validates_arguments_before_docker(
    launcher: tuple[Path, dict[str, str], Path], arguments: list[str],
) -> None:
    _, _, log = launcher
    assert _run(launcher, "pg-backup", *arguments).returncode == 2
    assert not _calls(log)



def test_postgres_backup_help_never_exports_a_file(
    launcher: tuple[Path, dict[str, str], Path],
) -> None:
    root, _, log = launcher
    result = _run(launcher, "pg-backup", "--help")
    assert result.returncode == 0 and "NEW_HOST_FILE" in result.stdout
    assert not _calls(log) and not (root / "--help").exists()
