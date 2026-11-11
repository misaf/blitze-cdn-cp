"""What changes between two rendered trees, path by path, as unified diffs."""

from __future__ import annotations

from blitzecdn.core.domain.configuration import (
    ConfigurationBundle,
    ConfigurationChangeKind,
    ConfigurationDiff,
)


def _bundle(files=None, links=None) -> ConfigurationBundle:
    return ConfigurationBundle(
        files={"nginx.conf": b"events {}\n", **(files or {})}, links=links or {}
    )


def test_identical_trees_have_no_changes():
    diff = ConfigurationDiff.between(_bundle(), _bundle())

    assert not diff.changed
    assert diff.changes == ()
    assert diff.patch() == ""
    assert diff.before == diff.after == _bundle().digest


def test_nothing_before_means_every_path_is_added():
    after = _bundle({"a.conf": b"x\n"}, {"l": "/t"})

    diff = ConfigurationDiff.between(None, after)

    assert diff.before is None
    assert [(c.path, c.change) for c in diff.changes] == [
        ("a.conf", ConfigurationChangeKind.ADDED),
        ("l", ConfigurationChangeKind.ADDED),
        ("nginx.conf", ConfigurationChangeKind.ADDED),
    ]
    assert diff.changes[0].diff == "--- /dev/null\n+++ b/a.conf\n@@ -0,0 +1 @@\n+x\n"


def test_edits_removals_and_retargeted_links_are_reported_by_path():
    before = _bundle({"gone.conf": b"old\n", "site.conf": b"a\nb\n"}, {"l": "/one"})
    after = _bundle({"site.conf": b"a\nc\n"}, {"l": "/two"})

    diff = ConfigurationDiff.between(before, after)

    assert [(c.path, c.change) for c in diff.changes] == [
        ("gone.conf", ConfigurationChangeKind.REMOVED),
        ("l", ConfigurationChangeKind.MODIFIED),
        ("site.conf", ConfigurationChangeKind.MODIFIED),
    ]
    patch = diff.patch()
    assert "--- a/gone.conf\n+++ /dev/null\n" in patch
    assert "--- a/l\n+++ b/l\n" in patch and "--> /one\n+-> /two\n" in patch
    assert "-b\n+c\n" in patch


def test_a_file_replaced_by_a_link_is_a_modification():
    before = _bundle({"x": b"content\n"})
    after = _bundle(links={"x": "/target"})

    (change,) = ConfigurationDiff.between(before, after).changes

    assert change.change is ConfigurationChangeKind.MODIFIED
    assert change.diff is not None and "+-> /target\n" in change.diff


def test_binary_content_is_reported_without_a_diff():
    before = _bundle({"key.der": b"\xff\x00"})
    after = _bundle({"key.der": b"\xfe\x00"})

    diff = ConfigurationDiff.between(before, after)

    assert [(c.path, c.diff) for c in diff.changes] == [("key.der", None)]
    assert diff.patch() == ""
    assert diff.summary()["changes"] == [
        {"path": "key.der", "change": "modified", "diff": None}
    ]


def test_a_missing_final_newline_is_marked_as_diff_marks_it():
    diff = ConfigurationDiff.between(
        _bundle({"a": b"one", "b": b"x\n"}), _bundle({"a": b"one\n", "b": b"y\n"})
    )

    (added_newline, _) = diff.changes
    assert added_newline.diff == (
        "--- a/a\n+++ b/a\n@@ -1 +1 @@\n-one\n\\ No newline at end of file\n+one\n"
    )
    # Never runs into the next file's header.
    assert "+one\n--- a/b\n" in diff.patch()
