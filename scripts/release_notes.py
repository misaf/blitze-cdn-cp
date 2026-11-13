"""Print a release's GitHub notes from its CHANGELOG.md section.

    python3 scripts/release_notes.py X.Y.Z [--changelog PATH]

Run through `just release-notes X.Y.Z`. The notes are the section itself,
reordered so every **BREAKING** entry comes first under "Breaking changes",
with the hard-wrapped lines joined (GitHub renders a single newline in a
release body as a line break), and the section's compare link at the end.

Standard library only, so it runs without the project's environment.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

_VERSION = re.compile(r"\d+\.\d+\.\d+")
_SECTION = re.compile(r"^## \[(?P<version>[^\]]+)\]", re.MULTILINE)
_LINK = re.compile(r"^\[(?P<version>[^\]]+)\]: (?P<url>\S+)$", re.MULTILINE)
_BREAKING = "**BREAKING:** "


class NotesError(Exception):
    """The changelog has no usable section for the requested version."""


@dataclass
class _Subsection:
    title: str
    entries: list[str] = field(default_factory=list)


def _join(lines: list[str]) -> str:
    """One paragraph or bullet, its wrapped lines joined with single spaces."""
    return " ".join(line.strip() for line in lines)


def _blocks(text: str) -> list[str]:
    """Split text into paragraphs and bullets, each joined onto one line."""
    blocks: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            if current:
                blocks.append(_join(current))
                current = []
        elif line.startswith("- ") and current:
            blocks.append(_join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        blocks.append(_join(current))
    return blocks


def section(changelog: str, version: str) -> str:
    """The raw text under `## [version]`, without its heading."""
    headings = list(_SECTION.finditer(changelog))
    for index, match in enumerate(headings):
        if match["version"] != version:
            continue
        start = changelog.index("\n", match.start()) + 1
        if index + 1 < len(headings):
            end = headings[index + 1].start()
        else:
            link = _LINK.search(changelog, start)
            end = link.start() if link else len(changelog)
        return changelog[start:end].strip()
    raise NotesError(f"CHANGELOG.md has no section for {version}")


def compare_url(changelog: str, version: str) -> str | None:
    """The link reference the changelog keeps for this version, if any."""
    for match in _LINK.finditer(changelog):
        if match["version"] == version:
            return match["url"]
    return None


def _split(body: str) -> tuple[list[str], list[_Subsection]]:
    """The section's opening paragraphs, and its `###` subsections in order."""
    first = re.search(r"(?m)^### ", body)
    intro, rest = (
        (body[: first.start()], body[first.start() :]) if first else (body, "")
    )
    subsections: list[_Subsection] = []
    for chunk in re.split(r"(?m)^### ", rest):
        if chunk.strip():
            title, _, text = chunk.partition("\n")
            subsections.append(_Subsection(title.strip(), _blocks(text)))
    return _blocks(intro), subsections


def _take_breaking(subsections: list[_Subsection]) -> list[str]:
    """Remove the BREAKING entries from each subsection and return them."""
    breaking: list[str] = []
    for sub in subsections:
        marked = [e for e in sub.entries if e.startswith("- " + _BREAKING)]
        sub.entries = [e for e in sub.entries if e not in marked]
        # Out of its subsection, a breaking removal needs its heading to read.
        label = "" if sub.title == "Changed" else f"{sub.title}: "
        breaking += [f"- {label}{e.removeprefix('- ' + _BREAKING)}" for e in marked]
    return breaking


def _render(sub: _Subsection) -> str:
    bullets = "\n".join(e for e in sub.entries if e.startswith("- "))
    prose = [e for e in sub.entries if not e.startswith("- ")]
    return f"### {sub.title}\n\n" + "\n\n".join([*prose, bullets] if bullets else prose)


def notes(changelog: str, version: str) -> str:
    """The GitHub release body for `version`."""
    body = section(changelog, version)
    if not body:
        raise NotesError(f"the {version} section is empty")
    intro, subsections = _split(body)
    breaking = _take_breaking(subsections)

    parts: list[str] = []
    if intro:
        parts.append("\n\n".join(intro))
    if breaking:
        parts.append("### Breaking changes\n\n" + "\n".join(breaking))
    parts += [_render(sub) for sub in subsections if sub.entries]
    url = compare_url(changelog, version)
    if url:
        parts.append(f"**Full Changelog**: {url}")
    return "\n\n".join(parts) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("version", help="MAJOR.MINOR.PATCH, without the v")
    parser.add_argument("--changelog", type=Path, default=Path("CHANGELOG.md"))
    args = parser.parse_args(argv)
    version: str = args.version.removeprefix("v")
    if not _VERSION.fullmatch(version):
        print(
            f"error: expected MAJOR.MINOR.PATCH, got {args.version!r}", file=sys.stderr
        )
        return 2
    try:
        sys.stdout.write(notes(args.changelog.read_text(), version))
    except NotesError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
