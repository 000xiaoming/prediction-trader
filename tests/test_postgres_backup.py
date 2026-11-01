"""Exercise the launcher with real PostgreSQL tools in a disposable, isolated container."""

import hashlib
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest


@pytest.mark.skipif(
    os.environ.get("GITHUB_ACTIONS") != "true"
    and os.environ.get("PREDICTION_TEST_DOCKER_BACKUP") != "1",
    reason="CI or PREDICTION_TEST_DOCKER_BACKUP=1 enables isolated Docker recovery drills",
)
def test_postgres_archive_restores_exact_data_in_an_isolated_container(tmp_path: Path) -> None:
    docker = shutil.which("docker")
    assert docker is not None, "Docker is required for the isolated backup drill"

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [docker, *args], capture_output=True, text=True, check=True, timeout=60,
        )

    container = run(
        "run", "--detach", "--rm", "--pull=never", "--network", "none",
        "--tmpfs", "/var/lib/postgresql/data", "--env", "POSTGRES_HOST_AUTH_METHOD=trust",
        "--env", "POSTGRES_USER=prediction", "--env", "POSTGRES_DB=prediction",
        "postgres:17-alpine",
    ).stdout.strip()
    assert re.fullmatch(r"[0-9a-f]{64}", container)
    try:
        deadline = time.monotonic() + 40
        while True:
            ready = subprocess.run(
                [docker, "exec", container, "sh", "-c",
                 'test "$(cat /proc/1/comm)" = postgres && pg_isready -U prediction -d prediction'],
                capture_output=True, timeout=10,
            )
            if ready.returncode == 0:
                break
            assert time.monotonic() < deadline, "Isolated PostgreSQL did not become ready"
            time.sleep(0.25)
        run("exec", container, "psql", "-U", "prediction", "-d", "prediction",
            "-v", "ON_ERROR_STOP=1", "-c",
            "CREATE TABLE example(id integer PRIMARY KEY, amount numeric, label text); "
            "INSERT INTO example VALUES(1, 0.1234567890123456789, '模拟数据');")
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        shutil.copyfile(Path(__file__).resolve().parents[1] / "pt", checkout / "pt")
        (checkout / ".env").write_text("")
        commands = tmp_path / "bin"
        commands.mkdir()
        # Only the Compose transport is replaced; pg_dump, pg_restore and hashing are real.
        wrapper = commands / "docker"
        wrapper.write_text(f"#!{sys.executable}\n" + f"""import os, sys
arguments = sys.argv[1:]
assert arguments[0] == 'compose', arguments
if 'exec' in arguments:
    rest = arguments[arguments.index('exec') + 1:]
    assert rest[:2] == ['-T', 'postgres'], rest
    os.execv({docker!r}, [{docker!r}, 'exec', '-i', {container!r}, *rest[2:]])
assert 'version' in arguments or 'config' in arguments, arguments
""")
        wrapper.chmod(0o755)
        target = checkout / "backups" / "postgres.dump"
        environment = dict(os.environ, PATH=str(commands) + os.pathsep + os.environ["PATH"])
        saved = subprocess.run(
            ["bash", str(checkout / "pt"), "--mode", "portable", "pg-backup", str(target)],
            env=environment, text=True, capture_output=True, timeout=60,
        )
        assert saved.returncode == 0, saved.stderr
        content = target.read_bytes()
        assert content.startswith(b"PGDMP")
        assert hashlib.sha256(content).hexdigest() in saved.stdout
        run("exec", container, "createdb", "-U", "prediction", "-T", "template0", "restore_check")
        subprocess.run(
            [docker, "exec", "-i", container, "pg_restore", "-U", "prediction",
             "-d", "restore_check",
             "--exit-on-error", "--single-transaction"],
            input=content, capture_output=True, check=True, timeout=60,
        )
        restored = run("exec", container, "psql", "-U", "prediction", "-d", "restore_check",
                       "-At", "-c", "SELECT id,amount,label FROM example ORDER BY id;")
        assert restored.stdout.strip() == "1|0.1234567890123456789|模拟数据"
    finally:
        run("rm", "--force", container)
