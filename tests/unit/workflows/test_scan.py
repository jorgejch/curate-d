"""How a scan tallies the per-folder action counts it reports."""

from curate_d.inventory.decisions import FileAction
from curate_d.workflows import scan


def test_a_first_folder_starts_the_tally() -> None:
    """An empty tally takes the folder's counts as they are."""
    counted = {FileAction.NEW: 2, FileAction.UNCHANGED: 5}

    assert scan._tally_actions({}, counted) == counted


def test_a_later_folder_adds_to_the_actions_it_shares_and_opens_the_rest() -> None:
    """Shared actions sum; an action only one side has keeps its count."""
    totals = {FileAction.NEW: 2, FileAction.UNCHANGED: 5}
    counted = {FileAction.NEW: 1, FileAction.CHANGED: 3}

    assert scan._tally_actions(totals, counted) == {
        FileAction.NEW: 3,
        FileAction.UNCHANGED: 5,
        FileAction.CHANGED: 3,
    }


def test_a_folder_with_nothing_counted_leaves_the_tally_as_it_was() -> None:
    """An empty folder adds nothing."""
    totals = {FileAction.NEW: 2}

    assert scan._tally_actions(totals, {}) == {FileAction.NEW: 2}


def test_the_tally_leaves_its_inputs_alone() -> None:
    """Neither mapping is written through: the tally is a new dict."""
    totals = {FileAction.NEW: 2}
    counted = {FileAction.NEW: 1}

    tallied = scan._tally_actions(totals, counted)

    assert tallied is not totals
    assert (totals, counted) == ({FileAction.NEW: 2}, {FileAction.NEW: 1})
