"""Fail when a tracked file is too big for Claude to read whole.

A file Claude has to read in pieces is a file where it misses the helper defined 900 lines
down, or edits the wrong one of two similar blocks. So every repo keeps its files under
these limits:

    code (.py .php .js .ts …)   800 lines
    CLAUDE.md                   300 lines or 20 KB, whichever comes first
    other Markdown             1000 lines

Files already over a limit when the check arrives are listed in `.file-size-baseline` with
their size at the time. They may shrink (the baseline is then lowered with
--update-baseline) but never grow, and a new file must fit the limit. Splitting the old
ones is refactoring work of its own; this only stops the problem getting worse.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

BASELINE = ".file-size-baseline"

CODE_LINES = 800
CLAUDE_MD_LINES = 300
CLAUDE_MD_BYTES = 20_000
DOC_LINES = 1000

CODE_SUFFIXES = {
    ".py", ".php", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".astro",
    ".css", ".sh", ".ps1",
}
_MINIFIED = re.compile(r"\.min\.[a-z]+$")
# Not .sql: the big SQL files are generated schema dumps, regenerated rather than edited,
# and a "never grow" rule would block their regeneration.


@dataclass(frozen=True)
class Size:
    lines: int
    bytes: int


@dataclass(frozen=True)
class Problem:
    path: str
    message: str


def limit_for(path: str) -> tuple[int, int | None] | None:
    """(max lines, max bytes or None) for a path, or None when it is not checked."""
    p = Path(path)
    if p.name == "CLAUDE.md":
        return CLAUDE_MD_LINES, CLAUDE_MD_BYTES
    if p.suffix.lower() == ".md":
        return DOC_LINES, None
    if p.suffix.lower() in CODE_SUFFIXES and not _MINIFIED.search(p.name.lower()):
        return CODE_LINES, None
    return None


def measure(data: bytes) -> Size:
    lines = data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)
    return Size(lines=lines, bytes=len(data))


def over(size: Size, limit: tuple[int, int | None]) -> bool:
    max_lines, max_bytes = limit
    return size.lines > max_lines or (max_bytes is not None and size.bytes > max_bytes)


def read_baseline(text: str) -> dict[str, Size]:
    """`path<TAB>lines<TAB>bytes` per line; `#` comments and blank lines ignored."""
    out: dict[str, Size] = {}
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip("\n")
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.rsplit("\t", 2)
        if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
            raise ValueError(
                f"{BASELINE} line {n} is not `path<TAB>lines<TAB>bytes`: {raw!r}. "
                f"Regenerate it with --update-baseline."
            )
        out[parts[0]] = Size(lines=int(parts[1]), bytes=int(parts[2]))
    return out


def write_baseline(entries: dict[str, Size]) -> str:
    head = (
        "# Files over the size limit when the check arrived: they may shrink, never grow.\n"
        "# Regenerate with: file-size-check --update-baseline  (path, lines, bytes)\n"
    )
    body = "".join(f"{p}\t{s.lines}\t{s.bytes}\n" for p, s in sorted(entries.items()))
    return head + body


def check(sizes: dict[str, Size], baseline: dict[str, Size]) -> list[Problem]:
    """Problems for the measured files, given the baseline. Pure: no disk, no git.

    The baseline is a ratchet: an entry for a file that has shrunk, or no longer exists,
    fails too, so the allowance is lowered (or dropped) in the same PR. Otherwise the file
    could grow back to its old size later, or a new file reuse a deleted one's allowance.
    """
    problems: list[Problem] = []
    for path, allowed in sorted(baseline.items()):
        size = sizes.get(path)
        if size is None:
            problems.append(Problem(
                path, f"is in {BASELINE} but no longer exists (or is no longer checked): "
                "run file-size-check --update-baseline to drop it."))
        elif size.lines < allowed.lines:
            problems.append(Problem(
                path, f"shrank to {size.lines} lines (baseline {allowed.lines}): run "
                "file-size-check --update-baseline to lower it, so it cannot grow back."))
    for path, size in sorted(sizes.items()):
        limit = limit_for(path)
        if limit is None or not over(size, limit):
            continue
        allowed = baseline.get(path)
        what = f"{size.lines} lines, {size.bytes:,} bytes"
        max_lines, max_bytes = limit
        cap = f"{max_lines} lines" + (f" / {max_bytes:,} bytes" if max_bytes else "")
        if allowed is None:
            problems.append(Problem(path, f"{what}: over the {cap} limit. Split it."))
        elif size.lines > allowed.lines or size.bytes > allowed.bytes:
            problems.append(Problem(
                path,
                f"{what}: grew past its baseline ({allowed.lines} lines, "
                f"{allowed.bytes:,} bytes). Already over {cap}: shrink it, don't add to it.",
            ))
    return problems


def tracked_files(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"], check=True, capture_output=True
    ).stdout
    return [os.fsdecode(p) for p in out.split(b"\0") if p]


def repo_root(start: str) -> Path:
    """The git toplevel, so running from a subdirectory checks the whole repo."""
    out = subprocess.run(
        ["git", "-C", start, "rev-parse", "--show-toplevel"],
        check=True, capture_output=True, text=True,
    ).stdout
    return Path(out.strip())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="file-size-check", description=__doc__.split("\n")[0])
    ap.add_argument("--root", default=".", help="any path inside the repo (default: .)")
    ap.add_argument(
        "--update-baseline", action="store_true",
        help=f"rewrite {BASELINE} with every file now over its limit (review the diff)",
    )
    args = ap.parse_args(argv)
    root = repo_root(args.root)

    sizes: dict[str, Size] = {}
    for path in tracked_files(root):
        if limit_for(path) is None:
            continue
        f = root / path
        if f.is_file() and not f.is_symlink():
            sizes[path] = measure(f.read_bytes())

    baseline_file = root / BASELINE
    if args.update_baseline:
        offenders = {pr.path: sizes[pr.path] for pr in check(sizes, {})}
        if offenders:
            baseline_file.write_text(write_baseline(offenders))
            print(f"{BASELINE}: {len(offenders)} file(s)")
        elif baseline_file.exists():
            baseline_file.unlink()
            print(f"{BASELINE}: nothing over the limits, removed")
        else:
            print("nothing over the limits")
        return 0

    try:
        baseline = read_baseline(baseline_file.read_text()) if baseline_file.exists() else {}
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1
    problems = check(sizes, baseline)
    for pr in problems:
        print(f"{pr.path}: {pr.message}", file=sys.stderr)
    if problems:
        return 1
    held = sum(1 for p in baseline if over(sizes[p], limit_for(p)))  # type: ignore[arg-type]
    print(f"file sizes OK: {len(sizes)} files checked"
          + (f", {held} over the limit held at their baseline" if held else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
