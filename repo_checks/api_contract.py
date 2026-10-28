"""Fail when a PHP repo calls an API route that the API does not have.

The PHP sites call the API by path, as a plain string (`amf_get('/events/' . $id)`), so a
route renamed or removed on the API side breaks a page with nothing to say so until someone
opens it. This reads every call in the repo, turns its first argument into a path pattern,
and checks it against the API's committed route list: one `METHOD /path/{param}` per line,
as amf-vps writes `tests/routes.txt` from its OpenAPI schema.

Which functions are API calls is the repo's own business, so each repo lists them in
`.api-contract`, one `function METHOD` per line:

    amf_get     GET
    amf_post    POST
    curl_init   ANY     # a raw URL: a computed start is the base URL, any method matches

What the path pattern is made of:
- String literals are the path. Anything computed (a variable, a cast, a call, `{$id}` in a
  double-quoted string) is one segment that matches anything.
- A variable assigned exactly once in the same file is replaced by what it was assigned,
  so `$line = '/orders/' . $id;  amf_post($line . '/pickup')` is `/orders/{?}/pickup`.
- A query string is not part of the path: everything from `?` is dropped, and so is a
  computed part glued onto a literal (`"/subs/{$id}/shipments{$qs}"`).
- A path that still starts with something computed (a variable from elsewhere) is checked
  by its literal end: some route must end that way.
- A call with no literal path at all cannot be checked. It fails, unless the line has a
  comment saying why: `// api-contract: skip <reason>`. A generic helper's own body (the
  function that every call goes through) is the usual case.
- A request made some other way (`file_get_contents($api . '/events')`) is declared with
  a comment on its line, `// api-contract: GET /events`, and checked like a call.

Comments and inline HTML are skipped, so a doc comment mentioning `amf_get()` is not a call.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

CONFIG = ".api-contract"
SKIP = "api-contract: skip"
_DECLARED = re.compile(r"api-contract:\s*(GET|POST|PUT|PATCH|DELETE)\s+(/\S*)")
METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "ANY"}
HOLE = "\x00"  # one computed part of a path
_WORD = re.compile(r"[A-Za-z_]\w*")


@dataclass(frozen=True)
class Call:
    file: str
    line: int
    method: str  # GET … DELETE, or ANY
    path: str | None  # the pattern, HOLE for each computed part; None: nothing literal


@dataclass(frozen=True)
class Problem:
    where: str
    message: str


# --- PHP source → code only ---------------------------------------------------------


def code_only(src: str) -> str:
    """src with inline HTML, comments and heredocs blanked to spaces (newlines kept), so
    offsets and line numbers still line up. String literals are kept as they are."""
    out = list(src)
    n = len(src)

    def blank(a: int, b: int) -> None:
        for k in range(a, min(b, n)):
            if out[k] != "\n":
                out[k] = " "

    i = 0
    while i < n:
        # Inline HTML, up to the next open tag.
        j = src.find("<?", i)
        if j < 0:
            blank(i, n)
            break
        open_len = 5 if src.startswith("<?php", j) else 3 if src.startswith("<?=", j) else 2
        blank(i, j + open_len)
        i = j + open_len
        # PHP code, up to the close tag.
        while i < n:
            c = src[i]
            if src.startswith("?>", i):
                i += 2
                break
            if c in "'\"`":
                i = _string_end(src, i)
            elif c == "#" and not src.startswith("#[", i) or src.startswith("//", i):
                j = i
                while j < n and src[j] != "\n" and not src.startswith("?>", j):
                    j += 1
                blank(i, j)
                i = j
            elif src.startswith("/*", i):
                j = src.find("*/", i + 2)
                j = n if j < 0 else j + 2
                blank(i, j)
                i = j
            elif src.startswith("<<<", i):
                m = re.compile(r"<<<[ \t]*(['\"]?)([A-Za-z_]\w*)\1\r?\n").match(src, i)
                if not m:
                    i += 3
                    continue
                end = re.compile(r"^[ \t]*" + m.group(2) + r"\b", re.M).search(src, m.end())
                j = n if end is None else end.end()
                blank(i, j)
                i = j
            else:
                i += 1
    return "".join(out)


def _string_end(src: str, i: int) -> int:
    """The index just past the string literal that starts at src[i]."""
    q = src[i]
    j = i + 1
    while j < len(src) and src[j] != q:
        j += 2 if src[j] == "\\" else 1
    return j + 1


# --- an argument → a path pattern ---------------------------------------------------


def _single_quoted(body: str) -> str:
    return re.sub(r"\\([\\'])", r"\1", body)


def _double_quoted(body: str) -> str:
    """Literal text, with HOLE for each interpolation."""
    out: list[str] = []
    j = 0
    while j < len(body):
        c = body[j]
        if c == "\\" and j + 1 < len(body):
            out.append(body[j + 1])
            j += 2
        elif c == "{" and body.startswith("{$", j):
            depth, k = 0, j
            while k < len(body):
                depth += {"{": 1, "}": -1}.get(body[k], 0)
                if depth == 0:
                    break
                k += 1
            out.append(HOLE)
            j = k + 1
        elif c == "$" and _WORD.match(body, j + 1):
            m = re.compile(r"\$\w+(\[[^\]]*\]|->\w+)?").match(body, j)
            assert m
            out.append(HOLE)
            j = m.end()
        else:
            out.append(c)
            j += 1
    return "".join(out)


class _Parser:
    """Reads one PHP expression (a call's first argument, or the right side of an
    assignment) as a path pattern. Works on code_only() text."""

    def __init__(self, code: str):
        self.code = code
        self._assigned: dict[str, list[int]] = {}
        # `.=` and `??=` count as assignments too, so a variable built up in steps is not
        # mistaken for its first part.
        for m in re.finditer(r"\$(\w+)\s*(\.|\?\?)?=(?![=>])", code):
            self._assigned.setdefault(m.group(1), []).append(m.end())

    def expr(self, i: int, stop: str, seen: frozenset[str] = frozenset()) -> str:
        out: list[str] = []
        depth = 0
        code = self.code
        n = len(code)

        def hole() -> None:
            if not out or out[-1] != HOLE:
                out.append(HOLE)

        while i < n:
            c = code[i]
            if c in "'\"":
                j = _string_end(code, i)
                if depth == 0:
                    body = code[i + 1 : j - 1]
                    text = _single_quoted(body) if c == "'" else _double_quoted(body)
                    out.append(text)
                i = j
            elif c in "([{":
                if depth == 0:
                    hole()
                depth += 1
                i += 1
            elif c in ")]}":
                if depth == 0:
                    break
                depth -= 1
                i += 1
            elif depth == 0 and c in stop:
                break
            elif c.isspace() or (c == "." and depth == 0):
                i += 1
            elif c == "$" and depth == 0 and (m := _WORD.match(code, i + 1)):
                name = m.group(0)
                j = m.end()
                rest = code[j:j + 2]
                spots = self._assigned.get(name, [])
                if (
                    len(spots) == 1 and name not in seen
                    and not rest.startswith(("[", "->", "("))
                ):
                    out.append(self.expr(spots[0], ";", seen | {name}))
                else:
                    hole()
                i = j
            else:
                if depth == 0:
                    hole()
                i += 1
        return re.sub(HOLE + "+", HOLE, "".join(out))


def normalize(raw: str, method: str) -> str | None:
    """A path pattern from an argument's text, or None when no part of it is literal."""
    path = raw.split("?", 1)[0]
    # A computed part glued onto a literal is a suffix (a query string), not a segment.
    path = re.sub("(?<=[^/" + HOLE + "])" + HOLE + ".*", "", path, flags=re.S)
    if method == "ANY":
        path = path.removeprefix(HOLE)  # the base URL
        if not path.startswith("/"):
            return None  # a URL somewhere else, or nothing literal
    if not path.replace(HOLE, "").strip("/"):
        return None
    return path


# --- matching -----------------------------------------------------------------------


def _segments(path: str) -> list[str]:
    return [s for s in path.split("/") if s != ""]


def _segment_ok(call: str, route: str) -> bool:
    if HOLE in call:
        return True
    return route.startswith("{") and route.endswith("}") or call == route


def matches(path: str, route_path: str) -> bool:
    route = _segments(route_path)
    if path.startswith(HOLE):  # an unknown start: compare the ends
        tail = _segments(path.split("/", 1)[1]) if "/" in path else []
        if not tail or len(tail) >= len(route):
            return False
        return all(_segment_ok(c, r) for c, r in zip(tail, route[-len(tail):], strict=True))
    call = _segments(path)
    return len(call) == len(route) and all(_segment_ok(c, r) for c, r in zip(call, route, strict=True))


def check(calls: list[Call], routes: list[tuple[str, str]]) -> list[Problem]:
    """Problems for the calls, given (method, path) routes. Pure: no disk."""
    problems: list[Problem] = []
    for c in calls:
        where = f"{c.file}:{c.line}"
        if c.path is None:
            problems.append(Problem(
                where, f"{c.method} with a computed path, which this check cannot read. "
                f"Write the path out, or add `// {SKIP} <why>` to the line."))
            continue
        if any(
            (c.method == "ANY" or m == c.method) and matches(c.path, p) for m, p in routes
        ):
            continue
        shown = c.path.replace(HOLE, "{…}")
        problems.append(Problem(where, f"{c.method} {shown}: the API has no such route."))
    return problems


# --- a repo → its calls -------------------------------------------------------------


def read_config(text: str) -> dict[str, str]:
    """`function METHOD` per line; `#` comments and blank lines ignored."""
    out: dict[str, str] = {}
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) != 2 or not _WORD.fullmatch(parts[0]) or parts[1] not in METHODS:
            raise ValueError(
                f"{CONFIG} line {n} is not `function METHOD` "
                f"(METHOD one of {', '.join(sorted(METHODS))}): {raw!r}"
            )
        out[parts[0]] = parts[1]
    return out


def read_routes(text: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for raw in text.splitlines():
        if raw.strip():
            method, path = raw.split(None, 1)
            out.append((method.upper(), path.strip()))
    return out


def find_calls(src: str, file: str, functions: dict[str, str]) -> list[Call]:
    code = code_only(src)
    lines = src.splitlines()
    parser = _Parser(code)
    call_re = re.compile(r"(?<![\w$>:\\])(" + "|".join(map(re.escape, functions)) + r")\s*\(")
    out: list[Call] = []
    for n, text in enumerate(lines, 1):
        if d := _DECLARED.search(text):
            path = re.sub(r"\{[^}/]*\}", HOLE, d.group(2))
            out.append(Call(file, n, d.group(1), path))
    for m in call_re.finditer(code):
        if re.search(r"\bfunction\s+$", code[max(0, m.start() - 20):m.start()]):
            continue  # the definition, not a call
        line = code.count("\n", 0, m.start()) + 1
        if SKIP in lines[line - 1] or _DECLARED.search(lines[line - 1]):
            continue
        method = functions[m.group(1)]
        path = normalize(parser.expr(m.end(), ","), method)
        if path is None and method == "ANY":
            continue  # a URL that is not the API's, or the client's own curl_init($url)
        out.append(Call(file, line, method, path))
    return out


def repo_calls(root: Path) -> list[Call]:
    functions = read_config((root / CONFIG).read_text())
    files = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--", "*.php"],
        check=True, capture_output=True, text=True,
    ).stdout.split("\0")
    calls: list[Call] = []
    for f in sorted(p for p in files if p):
        path = root / f
        if path.is_file() and not path.is_symlink():
            calls.extend(find_calls(path.read_text(errors="replace"), f, functions))
    return calls


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="api-contract-check", description=__doc__.split("\n")[0])
    ap.add_argument("--routes", required=True,
                    help="the API's route list, `METHOD /path` per line (amf-vps tests/routes.txt)")
    ap.add_argument("repos", nargs="*", default=["."],
                    help=f"PHP repos to check, each with a {CONFIG} (default: .)")
    args = ap.parse_args(argv)

    routes = read_routes(Path(args.routes).read_text())
    failed = False
    for repo in args.repos:
        root = Path(repo)
        try:
            calls = repo_calls(root)
        except (OSError, ValueError, subprocess.CalledProcessError) as e:
            print(f"{repo}: {e}", file=sys.stderr)
            failed = True
            continue
        problems = check(calls, routes)
        name = root.resolve().name
        for pr in problems:
            print(f"{name}/{pr.where}: {pr.message}", file=sys.stderr)
        if problems:
            failed = True
        else:
            print(f"{name}: API contract OK, {len(calls)} calls checked against {len(routes)} routes")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
