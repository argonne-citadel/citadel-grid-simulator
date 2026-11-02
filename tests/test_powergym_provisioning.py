"""Execute Dockerfile feeder provisioning against an offline Git fixture.

The remote default branch advances beyond the validated commit. Running the
actual Dockerfile shell block must copy the pinned feeder contents and reject
bad pins; no Docker daemon, network, or production feeder download is needed.
"""

import os
import shlex
import subprocess
from pathlib import Path

import pytest

DOCKERFILE = Path(__file__).parent.parent / "Dockerfile"


def git(repository: Path, *arguments: str) -> str:
    """Run local Git without depending on the user's commit identity/hooks."""
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull},
    )
    return result.stdout.strip()


@pytest.fixture
def remote(tmp_path: Path) -> tuple[Path, str]:
    repository = tmp_path / "remote"
    repository.mkdir()
    git(repository, "init", "--initial-branch=main")
    git(repository, "config", "user.name", "Provisioning test")
    git(repository, "config", "user.email", "test@example.invalid")
    git(repository, "config", "uploadpack.allowFilter", "true")
    for name in ("8500-Node", "123Bus"):
        feeder = repository / "systems" / name
        feeder.mkdir(parents=True)
        (feeder / "Master.dss").write_text("pinned feeder\n", encoding="utf-8")
    git(repository, "add", ".")
    git(repository, "commit", "-m", "Validated feeders")
    pin = git(repository, "rev-parse", "HEAD")
    for name in ("8500-Node", "123Bus"):
        (repository / "systems" / name / "Master.dss").write_text(
            "changed upstream feeder\n", encoding="utf-8"
        )
    git(repository, "commit", "-am", "Change default branch feeders")
    assert git(repository, "rev-parse", "HEAD") != pin
    return repository, pin


def provision(
    tmp_path: Path, remote_path: Path, pin: str
) -> subprocess.CompletedProcess[str]:
    """Run the real RUN block, substituting only sandbox paths and remote URL."""
    block = (
        DOCKERFILE.read_text(encoding="utf-8")
        .split("ARG POWERGYM_COMMIT=", 1)[1]
        .split("\nRUN ", 1)[1]
        .split("\n\n", 1)[0]
    )
    block = block.replace("\\\n", "")
    block = block.replace(
        "https://github.com/siemens/powergym.git", shlex.quote(str(remote_path))
    )
    block = block.replace("/tmp/powergym", shlex.quote(str(tmp_path / "checkout")))
    block = block.replace("/usr/app/examples", shlex.quote(str(tmp_path / "examples")))
    return subprocess.run(
        ["sh", "-c", block],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "POWERGYM_COMMIT": pin,
            "GIT_ALLOW_PROTOCOL": "file",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        },
    )


def test_provisioning_copies_pin_instead_of_advanced_default_branch(
    tmp_path: Path, remote: tuple[Path, str]
) -> None:
    remote_path, pin = remote
    result = provision(tmp_path, remote_path, pin)
    assert result.returncode == 0, result.stderr
    for feeder in ("ieee8500", "ieee123"):
        assert (tmp_path / "examples" / feeder / "Master.dss").read_text(
            encoding="utf-8"
        ) == "pinned feeder\n"


@pytest.mark.parametrize("pin", ["main", "a" * 39, "z" * 40, "0" * 40])
def test_invalid_or_unavailable_pin_never_copies_feeders(
    tmp_path: Path, remote: tuple[Path, str], pin: str
) -> None:
    remote_path, _ = remote
    result = provision(tmp_path, remote_path, pin)
    assert result.returncode != 0
    assert not (tmp_path / "examples").exists()
