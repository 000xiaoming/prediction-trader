"""Check Compose interpolation against temporary, nonsecret deployment files only."""

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest


def _compose_environment() -> dict[str, str]:
    # Retain executable/plugin discovery, but isolate all Compose interpolation
    # from operator credentials, COMPOSE_FILE, and the real active profile.
    allowed = {
        "PATH",
        "HOME",
        "USER",
        "USERPROFILE",
        "SYSTEMROOT",
        "WINDIR",
        "PATHEXT",
        "LOCALAPPDATA",
        "APPDATA",
        "PROGRAMDATA",
        "TMP",
        "TEMP",
        "DOCKER_CONFIG",
        "XDG_CONFIG_HOME",
    }
    return {key: value for key, value in os.environ.items() if key.upper() in allowed}


@pytest.fixture(scope="module")
def docker_command(tmp_path_factory: pytest.TempPathFactory) -> str:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker Compose is unavailable")
    try:
        result = subprocess.run(
            [docker, "compose", "version", "--short"],
            cwd=tmp_path_factory.mktemp("compose-version"),
            env=_compose_environment(),
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        pytest.skip("Docker Compose is unavailable")
    if result.returncode:
        pytest.skip("Docker Compose is unavailable")
    return docker


@pytest.fixture
def compose_repo(tmp_path: Path) -> Path:
    deployment = tmp_path / "deploy"
    deployment.mkdir()
    source = Path(__file__).resolve().parents[1] / "deploy"
    for filename in (
        "compose.yaml", "compose.profile.yaml", "compose.research.yaml", "compose.local.yaml"
    ):
        shutil.copyfile(source / filename, deployment / filename)
    (tmp_path / ".env").write_text("TRADING_PROFILE=polymarket_eth_5m\n", encoding="utf-8")
    (tmp_path / "data" / "research").mkdir(parents=True)
    return tmp_path


def _config(
    docker: str,
    repository: Path,
    *,
    from_deploy: bool,
    with_profile_file: bool = False,
    with_research_file: bool = False,
    with_local_file: bool = False,
    with_env_file: bool = False,
    shell_profile: str | None = None,
) -> subprocess.CompletedProcess[str]:
    command = [docker, "compose", "-f", str(repository / "deploy" / "compose.yaml")]
    if with_env_file:
        command.extend(["--env-file", str(repository / ".env")])
    if with_profile_file:
        command.extend(["-f", str(repository / "deploy" / "compose.profile.yaml")])
    if with_research_file:
        command.extend(["-f", str(repository / "deploy" / "compose.research.yaml")])
    if with_local_file:
        command.extend(["-f", str(repository / "deploy" / "compose.local.yaml")])
    command.extend(["config", "--format", "json"])
    environment = _compose_environment()
    if shell_profile is not None:
        environment["TRADING_PROFILE"] = shell_profile
    return subprocess.run(
        command,
        cwd=repository / "deploy" if from_deploy else repository,
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


def _trader_environment(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    assert result.returncode == 0, result.stderr
    config = json.loads(result.stdout)
    environment: dict[str, Any] = config["services"]["trader"]["environment"]
    return environment


def test_portable_compose_publishes_only_loopback_pages(
    docker_command: str, compose_repo: Path
) -> None:
    result = _config(docker_command, compose_repo, from_deploy=False)
    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)["services"]
    for service, target in (("trader", 8080), ("grafana", 3000)):
        ports = services[service]["ports"]
        assert len(ports) == 1
        assert ports[0]["host_ip"] == "127.0.0.1"
        assert ports[0]["target"] == target
        assert str(ports[0]["published"]) == str(target)
    assert not services["postgres"].get("ports")
    assert not services["prometheus"].get("ports")
    assert "HTTP_PROXY" not in services["trader"]["environment"]


def test_portable_ports_can_be_changed_without_exposing_other_interfaces(
    docker_command: str, compose_repo: Path
) -> None:
    (compose_repo / ".env").write_text(
        "INDICATOR_PUBLISHED_PORT=18080\nGRAFANA_PUBLISHED_PORT=13000\n", encoding="utf-8"
    )
    result = _config(docker_command, compose_repo, from_deploy=True, with_env_file=True)
    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)["services"]
    for service, expected in (("trader", "18080"), ("grafana", "13000")):
        assert str(services[service]["ports"][0]["published"]) == expected
        assert services[service]["ports"][0]["host_ip"] == "127.0.0.1"


def test_restored_container_ledger_has_an_independent_path_selector(
    docker_command: str, compose_repo: Path,
) -> None:
    (compose_repo / ".env").write_text("PAPER_LEDGER_PATH=data/local-only.db\n")
    result = _config(docker_command, compose_repo, from_deploy=False, with_env_file=True)
    assert _trader_environment(result)["PAPER_LEDGER_PATH"] == "/data/paper-ledger.sqlite3"
    (compose_repo / ".env").write_text(
        "PAPER_LEDGER_PATH=data/local-only.db\n"
        "PAPER_LEDGER_CONTAINER_PATH=/data/recovered-paper.sqlite3\n",
    )
    result = _config(docker_command, compose_repo, from_deploy=False, with_env_file=True)
    assert _trader_environment(result)["PAPER_LEDGER_PATH"] == "/data/recovered-paper.sqlite3"


def test_wsl_overlay_replaces_instead_of_adding_published_ports(
    docker_command: str, compose_repo: Path
) -> None:
    result = _config(docker_command, compose_repo, from_deploy=False, with_local_file=True)
    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)["services"]
    for service, published in (("trader", "13080"), ("grafana", "13000")):
        ports = services[service]["ports"]
        assert len(ports) == 1
        assert ports[0]["host_ip"] == "198.18.0.1"
        assert str(ports[0]["published"]) == published
    assert services["trader"]["environment"]["TRADING_MODE"] == "paper"


@pytest.mark.parametrize("from_deploy", [False, True], ids=["repo-cwd", "deploy-cwd"])
@pytest.mark.parametrize("shell_profile", [None, "polymarket_btc_15m"])
def test_base_compose_uses_env_file_profile_from_either_directory(
    docker_command: str,
    compose_repo: Path,
    from_deploy: bool,
    shell_profile: str | None,
) -> None:
    environment = _trader_environment(
        _config(
            docker_command,
            compose_repo,
            from_deploy=from_deploy,
            shell_profile=shell_profile,
        )
    )
    assert environment["TRADING_PROFILE"] == "polymarket_eth_5m"
    assert environment["TRADING_MODE"] == "paper"


@pytest.mark.parametrize("from_deploy", [False, True], ids=["repo-cwd", "deploy-cwd"])
def test_profile_override_applies_shell_selector(
    docker_command: str, compose_repo: Path, from_deploy: bool
) -> None:
    environment = _trader_environment(
        _config(
            docker_command,
            compose_repo,
            from_deploy=from_deploy,
            with_profile_file=True,
            shell_profile="polymarket_btc_15m",
        )
    )
    assert environment["TRADING_PROFILE"] == "polymarket_btc_15m"


@pytest.mark.parametrize("from_deploy", [False, True], ids=["repo-cwd", "deploy-cwd"])
def test_profile_override_can_use_explicit_root_dotenv_for_interpolation(
    docker_command: str, compose_repo: Path, from_deploy: bool
) -> None:
    environment = _trader_environment(
        _config(
            docker_command,
            compose_repo,
            from_deploy=from_deploy,
            with_profile_file=True,
            with_env_file=True,
        )
    )
    assert environment["TRADING_PROFILE"] == "polymarket_eth_5m"


def test_profile_override_from_deploy_requires_interpolation_selector(
    docker_command: str, compose_repo: Path
) -> None:
    # Service env_file is not an interpolation source when cwd is deploy/.
    result = _config(docker_command, compose_repo, from_deploy=True, with_profile_file=True)
    assert result.returncode != 0
    assert "Set TRADING_PROFILE explicitly for this override" in result.stderr


@pytest.mark.parametrize("from_deploy", [False, True], ids=["repo-cwd", "deploy-cwd"])
def test_profile_override_rejects_missing_selector(
    docker_command: str, compose_repo: Path, from_deploy: bool
) -> None:
    (compose_repo / ".env").write_text("", encoding="utf-8")
    result = _config(docker_command, compose_repo, from_deploy=from_deploy, with_profile_file=True)
    assert result.returncode != 0
    assert "Set TRADING_PROFILE explicitly for this override" in result.stderr


@pytest.mark.parametrize("from_deploy", [False, True], ids=["repo-cwd", "deploy-cwd"])
def test_base_compose_preserves_application_default_when_selector_is_absent(
    docker_command: str, compose_repo: Path, from_deploy: bool
) -> None:
    (compose_repo / ".env").write_text("", encoding="utf-8")
    environment = _trader_environment(
        _config(docker_command, compose_repo, from_deploy=from_deploy)
    )
    assert "TRADING_PROFILE" not in environment


@pytest.mark.parametrize("from_deploy", [False, True], ids=["repo-cwd", "deploy-cwd"])
@pytest.mark.parametrize("with_profile_file", [False, True])
def test_research_override_preserves_symbol_placeholder_and_profile_selection(
    docker_command: str,
    compose_repo: Path,
    from_deploy: bool,
    with_profile_file: bool,
) -> None:
    environment = _trader_environment(
        _config(
            docker_command,
            compose_repo,
            from_deploy=from_deploy,
            with_profile_file=with_profile_file,
            with_research_file=True,
            shell_profile="polymarket_btc_15m",
        )
    )
    assert environment["RESEARCH_CONTEXT_PATH"] == "/research/{symbol}.json"
    assert environment["TRADING_PROFILE"] == (
        "polymarket_btc_15m" if with_profile_file else "polymarket_eth_5m"
    )
