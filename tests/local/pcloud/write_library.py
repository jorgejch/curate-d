"""Write the fixture library to a directory, with a manifest of where each file should land.

Run from the repository root: `uv run python -m tests.local.pcloud.write_library DIR [TAG]`.
The manifest, `DIR/../<DIR name>-manifest.json`, lists each file's path, size, SHA-256 and
expected locator (null for what stays unresolved under the seed taxonomy).
"""

import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from tests.integration.fixture_library import library


def main(directory: Path, tag: str) -> Path:
    """Write every fixture under `directory` and return the manifest's path."""
    manifest = []
    for fixture in library(tag).fixtures:
        target = directory / fixture.path.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(fixture.content)
        manifest.append(
            {
                "path": fixture.path,
                "bytes": len(fixture.content),
                "sha256": hashlib.sha256(fixture.content).hexdigest(),
                "locator": fixture.locator,
            }
        )
    written = directory.with_name(f"{directory.name}-manifest.json")
    written.write_text(json.dumps({"tag": tag, "files": manifest}, indent=2) + "\n")
    return written


if __name__ == "__main__":
    default_tag = f"dev-{datetime.now(UTC):%Y%m%d%H%M%S}"
    print(main(Path(sys.argv[1]), sys.argv[2] if len(sys.argv) > 2 else default_tag))
