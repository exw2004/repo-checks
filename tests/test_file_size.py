"""file_size.check and friends: pure logic, plus one end-to-end run on a temp git repo."""
"""Run through `make test`, which installs this checkout as the package."""
import subprocess
import tempfile
from pathlib import Path

from repo_checks.file_size import (
    Size, check, limit_for, main, measure, read_baseline, write_baseline,
)

# --- which limit applies ------------------------------------------------------
assert limit_for("api/routers/x.py") == (800, None)
assert limit_for("docs/CLAUDE.md") == (300, 20_000)
assert limit_for("README.md") == (1000, None)
assert limit_for("uv.lock") is None and limit_for("data/x.csv") is None
assert limit_for("sql/basil_reference.sql") is None  # generated dumps are not checked
assert limit_for("assets/app.min.js") is None
assert limit_for("vendor/LIB.MIN.MJS") is None and limit_for("x.min.cjs") is None
print("limits by path OK")

# --- measuring ----------------------------------------------------------------
assert measure(b"") == Size(0, 0)
assert measure(b"a\nb\n") == Size(2, 4)
assert measure(b"a\nb") == Size(2, 3)  # a last line without a newline still counts
print("measure OK")


def big(n: int) -> Size:
    return Size(n, n * 10)


# --- new files ----------------------------------------------------------------
assert check({"a.py": big(800)}, {}) == []  # at the limit is fine
p = check({"a.py": big(801)}, {})
assert len(p) == 1 and "Split it" in p[0].message
# CLAUDE.md fails on bytes even when short on lines
p = check({"CLAUDE.md": Size(200, 25_000)}, {})
assert len(p) == 1 and "300 lines / 20,000 bytes" in p[0].message
print("new files over the limit fail")

# --- baselined files: may shrink, never grow ----------------------------------
base = {"old.py": Size(1200, 40_000)}
assert check({"old.py": Size(1200, 40_000)}, base) == []
p = check({"old.py": Size(1100, 39_000)}, base)  # shrank: the entry must come down too
assert len(p) == 1 and "lower it" in p[0].message
p = check({}, base)  # deleted: its allowance must not wait for a new file at that path
assert len(p) == 1 and "no longer exists" in p[0].message
p = check({"old.py": Size(1201, 40_010)}, base)
assert len(p) == 1 and "grew past its baseline" in p[0].message
p = check({"old.py": Size(1200, 40_001)}, base)  # longer lines count too
assert len(p) == 1
print("baseline ratchet: growth, shrinking without lowering, and stale entries fail")

# --- baseline file round trip -------------------------------------------------
text = write_baseline({"b.py": Size(900, 1), "a.md": Size(1500, 2)})
assert text.index("a.md") < text.index("b.py")  # sorted, so diffs stay small
assert read_baseline(text) == {"b.py": Size(900, 1), "a.md": Size(1500, 2)}
try:
    read_baseline("a.py    900    1\n")  # tabs turned into spaces
    raise SystemExit("a malformed baseline was accepted")
except ValueError as e:
    assert "line 1" in str(e)
print("baseline round trip OK")

# --- end to end on a real git repo --------------------------------------------
with tempfile.TemporaryDirectory() as d:
    r = Path(d)
    git = ["git", "-C", d]
    subprocess.run(["git", "init", "-q", d], check=True)
    (r / "ok.py").write_text("x = 1\n" * 10)
    (r / "huge.py").write_text("x = 1\n" * 900)
    (r / "untracked.py").write_text("x = 1\n" * 900)  # not in git: ignored
    subprocess.run([*git, "add", "ok.py", "huge.py"], check=True)
    assert main(["--root", d]) == 1  # huge.py, new, over
    assert main(["--root", d, "--update-baseline"]) == 0
    assert "huge.py\t900\t" in (r / ".file-size-baseline").read_text()
    assert main(["--root", d]) == 0  # held at its baseline
    (r / "sub").mkdir()
    assert main(["--root", str(r / "sub")]) == 0  # run from a subdirectory: same repo
    (r / "huge.py").write_text("x = 1\n" * 901)
    assert main(["--root", d]) == 1  # grew
    (r / "huge.py").write_text("x = 1\n" * 10)
    assert main(["--root", d]) == 1  # shrank below its entry: must be lowered
    assert main(["--root", d, "--update-baseline"]) == 0
    assert not (r / ".file-size-baseline").exists()  # nothing over: baseline removed
print("end to end OK")
