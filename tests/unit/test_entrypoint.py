"""The console entry point."""

import pytest

from curate_d import main


def test_main_exits_with_not_implemented_message() -> None:
    """The console entry point should fail loudly rather than appear to succeed."""
    with pytest.raises(SystemExit) as exc_info:
        main()

    assert "not yet implemented" in str(exc_info.value)
