# repo-checks

Checks every repo runs the same way, in CI and locally, pinned to a release tag:

```bash
uvx --from git+https://github.com/exw2004/repo-checks@v0.1.0 file-size-check
```

## file-size-check

Fails when a tracked file is too big for Claude to read whole: a file read in pieces is
where it misses the helper defined 900 lines down, or edits the wrong one of two similar
blocks.

| File | Limit |
|---|---|
| Code (`.py .php .js .mjs .cjs .jsx .ts .tsx .astro .css .sh .ps1`) | 800 lines |
| `CLAUDE.md` | 300 lines or 20 KB, whichever comes first |
| Other Markdown | 1,000 lines |

Minified files, SQL (mostly generated schema dumps), lockfiles, data and anything untracked
are not checked.

Files already over a limit when a repo adopts the check go in `.file-size-baseline` (path,
lines, bytes). It is a ratchet: a baselined file may not grow, and when one shrinks (or is
deleted) the same PR lowers (or drops) its entry, or the check fails, so it can never grow
back. A new file must fit. Splitting the old ones is refactoring work of its own; the check
only stops it getting worse.

```bash
file-size-check                    # check (exit 1 on a problem)
file-size-check --update-baseline  # rewrite the baseline from today's files; review the diff
```

Run `--update-baseline` when adopting the check, and after shrinking or deleting a
baselined file. Never to let a file grow: that would show in the PR's diff.

## Semgrep rules

Rules for traps a linter can see, shared by every repo they apply to. Each `semgrep/<lang>.yml`
has its test cases beside it in `semgrep/<lang>.py` (or `.php`), checked by `make test`.
A repo pins the file by tag, as a URL:

```make
SEMGREP_SHARED := https://raw.githubusercontent.com/exw2004/repo-checks/v0.2.0/semgrep

audit:
	uvx --python 3.13 semgrep==1.181.0 scan --config p/python \
		--config $(SEMGREP_SHARED)/python.yml --metrics off --error .
```

| Rule | Catches |
|---|---|
| `python.yml` `request-value-in-sql-string` | A FastAPI route parameter reaching the SQL text of a PyMySQL `execute()` / `executemany()`, through f-strings, `+` or `.format`. A placeholder list sized with `len()` and a lookup in an UPPER_CASE allowlist dict are allowed. |

## api-contract-check

Fails when a PHP repo calls an API route the API does not have. The sites call amf-vps by
path, as a plain string, so a route renamed or removed on the API side breaks a page
silently. This reads every call, turns its path argument into a pattern (string literals
are the path, anything computed is one segment), and checks it against the API's route
list, `METHOD /path/{param}` per line, as amf-vps commits it in `tests/routes.txt`.

```bash
api-contract-check --routes ../amf-vps/tests/routes.txt          # this repo
api-contract-check --routes tests/routes.txt ../site ../app      # from the API's side
```

Each PHP repo lists its client functions in `.api-contract`, `function METHOD` per line
(`ANY` for a raw URL such as `curl_init`, where a computed start is the base URL):

```
amf_get     GET
amf_post    POST
curl_init   ANY
```

Comments and inline HTML are skipped. A variable assigned once in the same file is read
through (`$line = '/orders/' . $id; amf_post($line . '/pickup')`), a query string is
dropped, and a path that still starts with something computed is matched on its end.
Two comments cover the rest, on the call's line:

| Comment | For |
|---|---|
| `// api-contract: skip <why>` | A call whose path is all computed, such as the client's own pass-through. Without it, such a call fails |
| `// api-contract: GET /events` | A request made another way (`file_get_contents($api . '/events')`), checked like a call |

Only paths and methods are checked, not request or response fields.

## In a repo

```make
REPO_CHECKS := git+https://github.com/exw2004/repo-checks@v0.1.0

size:  ## Files small enough for Claude to read whole
	uvx --python 3.13 --from $(REPO_CHECKS) file-size-check

check: lint test size audit
```

## Releases

Repos pin a tag, so a change is a PR, then a new tag `vX.Y.Z` matching `version` in
`pyproject.toml` (and `__version__`). Release tags cannot be moved or deleted.
