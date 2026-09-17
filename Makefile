# Validation entrypoints for this repository.
#
# Everything here is the standard library and nothing else. `make test`
# is the whole check: the public-repo guard first (nothing private may
# appear in or be required by this repository), then the docs structure
# guard (the V4 path is the only active path), the cleanup-spec digest
# check, the script unit tests, and the archived V2 reference backend's
# hermetic suite (archived means frozen, not broken). If it passes, the
# tree is publishable.

PYTHON ?= python3

.PHONY: help site test guard docs spec reference-test reference-go reference-python

help:
	@echo "make site   assemble the deployable static site in _site/"
	@echo "make test   guard + docs check + spec check + script tests + archived backend suite (Python $(PYTHON), no dependencies)"
	@echo "make guard  just the public-repo guard (also runs first in make test)"
	@echo "make docs   just the docs structure guard"
	@echo "make spec   regenerate the derived cleanup-spec files in spec/cleanup/v1/"
	@echo "make reference-test  just the V4 reference implementations (Go + Python)"

# docs/ is the whole published tree. Retired pre-V4 paths (the old
# /legacy/ pages, openapi.yaml, agent-prompts/, integrations/) exist in
# docs/ only as redirect stubs and short retirement notices; the archived
# material itself lives under legacy/ and is not published.
# scripts/docs_check.py mirrors this layout in EXTRA_SITE_FILES — change
# both together.
site:
	rm -rf _site
	mkdir -p _site/spec/cleanup/v1
	cp -R docs/. _site/
	cp spec/cleanup/v1/* _site/spec/cleanup/v1/
	$(PYTHON) scripts/public_guard.py
	$(PYTHON) scripts/docs_check.py --site _site

guard:
	$(PYTHON) scripts/public_guard.py

docs:
	$(PYTHON) scripts/docs_check.py

spec:
	$(PYTHON) scripts/build_cleanup_spec.py

test: guard
	$(PYTHON) scripts/docs_check.py
	$(PYTHON) scripts/build_cleanup_spec.py --check
	$(PYTHON) -m unittest discover -s scripts -p 'test_*.py'
	$(MAKE) reference-test
	cd legacy/reference-backend && $(PYTHON) -m unittest discover -s tests

reference-test: reference-go reference-python

reference-go:
	@if command -v go >/dev/null 2>&1; then \
		cd reference/go && go vet ./... && go test ./...; \
	else \
		echo "reference-go: no Go toolchain — skipping (install Go to run the recommended implementation's suite)"; \
	fi

reference-python:
	cd reference/python && $(PYTHON) -m unittest discover -s tests
