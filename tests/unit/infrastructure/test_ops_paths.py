"""Operational assets and their live repository entry points."""

import re
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize(
    "relative_path",
    [
        "ops/postgres/initdb/01-extensions.sql",
        "ops/rustfs/initialize.sh",
        "ops/rustfs/render_policy.sh",
        "ops/reset/reset-environment.sh",
        "ops/reset/reset.py",
        "ops/staging-compatibility.md",
    ],
)
def test_operational_entry_points_exist(relative_path: str) -> None:
    """Operational commands and mounted assets resolve under ops/."""
    path = REPOSITORY_ROOT / relative_path
    assert path.is_file(), f"Missing operational asset: {relative_path}"


@pytest.mark.parametrize(
    "relative_path", ["ops/reset/reset-environment.sh", "ops/rustfs/initialize.sh"]
)
def test_operational_scripts_remain_executable(relative_path: str) -> None:
    """Directly invoked operational scripts retain their executable modes after the move."""
    path = REPOSITORY_ROOT / relative_path
    assert path.stat().st_mode & 0o111, f"Operational script is not executable: {relative_path}"


@pytest.mark.parametrize(
    "relative_path",
    [
        "compose.yaml",
        ".dockerignore",
        ".github/workflows/integration.yml",
        "sonar-project.properties",
        "ops/reset/reset-environment.sh",
        "README.md",
        "CONTRIBUTING.md",
        "docs/reset.md",
        "docs/recovery.md",
        "docs/staging.md",
        "ops/README.md",
        "ops/staging-compatibility.md",
        "docs/diagrams/architecture/curate-d-architecture.json",
        "docs/diagrams/architecture/curate-d-architecture.html",
    ],
)
def test_live_entry_points_do_not_reference_old_root(relative_path: str) -> None:
    """Live commands use ops/ while the unrelated unit-test grouping keeps its name."""
    text = (REPOSITORY_ROOT / relative_path).read_text(encoding="utf-8")
    stale = re.findall(r"(?<!tests/unit/)infrastructure/", text)
    assert not stale, f"{relative_path} still references the old operational root"
