"""The local staging bucket and prefix: literal in compose, granted by the RustFS policies."""

import json
from pathlib import Path
from typing import cast

import pytest
import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
COMPOSE = REPOSITORY_ROOT / "compose.yaml"
POLICIES = REPOSITORY_ROOT / "infrastructure" / "rustfs" / "policies"

WORKERS = ("worker-io", "worker-cpu")
BUCKET = "CURATE_D_STAGING_S3_BUCKET"
PREFIX = "CURATE_D_STAGING_S3_CONTENT_PREFIX"

type Document = dict[str, object]


def _services() -> dict[str, Document]:
    """Return compose.yaml's services, with merge keys resolved."""
    compose = cast(Document, yaml.safe_load(COMPOSE.read_text(encoding="utf-8")))
    return cast(dict[str, Document], compose["services"])


def _environment(service: str) -> dict[str, str]:
    """Return a compose service's environment mapping."""
    return cast(dict[str, str], _services()[service].get("environment", {}))


def _resources(policy: Path) -> set[str]:
    """Return every resource ARN a policy document's statements name."""
    document = cast(Document, json.loads(policy.read_text(encoding="utf-8")))
    resources: set[str] = set()
    for statement in cast(list[Document], document["Statement"]):
        resource = statement["Resource"]
        resources.update([resource] if isinstance(resource, str) else cast(list[str], resource))
    return resources


def _policies() -> list[Path]:
    """Return the RustFS policy files rustfs-init attaches."""
    return sorted(POLICIES.glob("*.json"))


@pytest.mark.parametrize("worker", WORKERS)
@pytest.mark.parametrize("setting", [BUCKET, PREFIX])
def test_workers_stage_in_a_literal_bucket_and_prefix(worker: str, setting: str) -> None:
    """No shell variable can move a worker's staging bucket or prefix off the policies' grant."""
    value = _environment(worker)[setting]
    assert "$" not in value, f"{worker} interpolates {setting}: {value!r}"


def test_both_workers_stage_in_the_same_place() -> None:
    """The writer stages where the reader reads."""
    io, cpu = (_environment(worker) for worker in WORKERS)
    assert (io[BUCKET], io[PREFIX]) == (cpu[BUCKET], cpu[PREFIX])


@pytest.mark.parametrize("policy", _policies(), ids=lambda path: path.name)
def test_the_policies_grant_exactly_the_workers_bucket_and_prefix(policy: Path) -> None:
    """A policy grants the compose prefix, and nothing outside it but its bucket."""
    environment = _environment("worker-io")
    bucket, prefix = environment[BUCKET], environment[PREFIX]
    grant = f"arn:aws:s3:::{bucket}/{prefix}*"
    allowed = {
        f"arn:aws:s3:::{bucket}",
        # RustFS checks the key `content/` as `content`; see initialize.sh.
        f"arn:aws:s3:::{bucket}/{prefix.rstrip('/')}",
        grant,
    }
    resources = _resources(policy)
    assert grant in resources
    assert resources <= allowed, f"{policy.name} grants {sorted(resources - allowed)}"


def test_provisioning_takes_no_bucket_or_prefix_input() -> None:
    """rustfs-init reads the bucket and prefix from the policies, never from its environment."""
    environment = _environment("rustfs-init")
    assert not {"RUSTFS_STAGING_BUCKET", "RUSTFS_STAGING_PREFIX"} & environment.keys()
