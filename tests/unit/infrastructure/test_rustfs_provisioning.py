"""The local staging bucket and prefix: one variable for workers and provisioning, rendered into the policies."""

import json
import subprocess
from pathlib import Path
from typing import cast

import pytest
import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
COMPOSE = REPOSITORY_ROOT / "compose.yaml"
RUSTFS = REPOSITORY_ROOT / "infrastructure" / "rustfs"
POLICIES = RUSTFS / "policies"
RENDER = RUSTFS / "render_policy.sh"

WORKERS = ("worker-io", "worker-cpu")
BUCKET = "CURATE_D_STAGING_S3_BUCKET"
PREFIX = "CURATE_D_STAGING_S3_CONTENT_PREFIX"
BUCKET_INPUT = "${CURATE_D_STAGING_S3_BUCKET:-curate-d-staging}"
PREFIX_INPUT = "${CURATE_D_STAGING_S3_CONTENT_PREFIX:-content/}"

type Document = dict[str, object]


def _services() -> dict[str, Document]:
    """Return compose.yaml's services, with merge keys resolved."""
    compose = cast(Document, yaml.safe_load(COMPOSE.read_text(encoding="utf-8")))
    return cast(dict[str, Document], compose["services"])


def _environment(service: str) -> dict[str, str]:
    """Return a compose service's environment mapping."""
    return cast(dict[str, str], _services()[service].get("environment", {}))


def _policies() -> list[Path]:
    """Return the RustFS policy templates rustfs-init renders and attaches."""
    return sorted(POLICIES.glob("*.json"))


def _render(template: Path, bucket: str, prefix: str) -> subprocess.CompletedProcess[str]:
    """Run render_policy.sh on a template and capture its output."""
    return subprocess.run(
        ["sh", str(RENDER), str(template), bucket, prefix],
        capture_output=True,
        text=True,
        check=False,
    )


def _resources(document: Document) -> set[str]:
    """Return every resource ARN a policy document's statements name."""
    resources: set[str] = set()
    for statement in cast(list[Document], document["Statement"]):
        resource = statement["Resource"]
        resources.update([resource] if isinstance(resource, str) else cast(list[str], resource))
    return resources


@pytest.mark.parametrize("worker", WORKERS)
def test_workers_take_the_bucket_and_prefix_from_the_shared_variables(worker: str) -> None:
    """Each worker reads the bucket and prefix from the variables rustfs-init reads, with the same defaults."""
    environment = _environment(worker)
    assert environment[BUCKET] == BUCKET_INPUT
    assert environment[PREFIX] == PREFIX_INPUT


def test_provisioning_is_given_the_workers_bucket_and_prefix() -> None:
    """rustfs-init provisions the bucket and prefix the workers stage in."""
    environment = _environment("rustfs-init")
    assert environment["RUSTFS_STAGING_BUCKET"] == BUCKET_INPUT
    assert environment["RUSTFS_STAGING_PREFIX"] == PREFIX_INPUT


@pytest.mark.parametrize("policy", _policies(), ids=lambda path: path.name)
@pytest.mark.parametrize(
    ("bucket", "prefix"),
    [("curate-d-staging", "content/"), ("other.bucket-1", "a/b_c/")],
)
def test_a_rendered_policy_grants_exactly_the_bucket_and_prefix(
    policy: Path, bucket: str, prefix: str
) -> None:
    """A rendered policy is valid JSON granting the prefix, its slashless key and the bucket, nothing more."""
    result = _render(policy, bucket, prefix)
    assert result.returncode == 0, result.stderr
    assert "@" not in result.stdout
    grant = f"arn:aws:s3:::{bucket}/{prefix}*"
    allowed = {f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/{prefix.rstrip('/')}", grant}
    resources = _resources(cast(Document, json.loads(result.stdout)))
    assert grant in resources
    assert resources <= allowed, f"{policy.name} grants {sorted(resources - allowed)}"


@pytest.mark.parametrize(
    "bucket",
    ["", "ab", "Upper", "-lead", "trail-", "has space", "a/b", "a&b", "x" * 64],
)
def test_render_rejects_an_unusable_bucket(bucket: str) -> None:
    """A bucket outside S3's naming rules never reaches a policy."""
    result = _render(_policies()[0], bucket, "content/")
    assert result.returncode != 0
    assert "invalid bucket name" in result.stderr


@pytest.mark.parametrize(
    "prefix",
    ["", "content", "/content/", "a//b/", "a b/", "a*/", "a|b/", "a&b/", '"/'],
)
def test_render_rejects_an_unusable_prefix(prefix: str) -> None:
    """A prefix that is empty, rooted, unslashed or outside the key alphabet never reaches a policy."""
    result = _render(_policies()[0], "curate-d-staging", prefix)
    assert result.returncode != 0
    assert "invalid prefix" in result.stderr
