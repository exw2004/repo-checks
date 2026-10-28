# Same targets as every repo: test, lint, audit, check. `make check` is what CI runs.
CONTAINER ?= podman
GITLEAKS_IMAGE := ghcr.io/gitleaks/gitleaks:v8.24.3
RUFF := ruff==0.17.1
SEMGREP := semgrep==1.181.0
PYTHON := 3.13
RUN = uv run --isolated --python $(PYTHON) --with-editable .

.PHONY: test lint audit size check

test:  ## Every test script, against this checkout installed as a package; every semgrep rule against its annotated cases
	@for t in tests/test_*.py; do echo "== $$t"; $(RUN) python "$$t" || exit 1; done
	uvx --python $(PYTHON) $(SEMGREP) scan --test semgrep/ --metrics off

lint:  ## ruff: syntax errors, unused names, likely bugs
	uvx --python $(PYTHON) $(RUFF) check --select E9,F,B --exclude semgrep .

audit:  ## Committed secrets, over the whole history
	$(CONTAINER) run --rm -v "$(CURDIR):/repo:ro" --security-opt label=disable \
		$(GITLEAKS_IMAGE) git /repo --no-banner --redact

size:  ## This repo's own files, checked by this checkout
	$(RUN) file-size-check

check: lint test size audit
