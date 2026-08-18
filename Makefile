# Validation entrypoints for this repository.
#
# Everything here is the standard library and nothing else — the same
# requirement the reference backend itself has. `make test` is the whole
# check: the public-repo guard first (nothing private may appear in or be
# required by this repository), then the reference backend and agent
# bridge suites. If it passes, the backend honours the contract in
# openapi.yaml and the tree is publishable.

PYTHON ?= python3

.PHONY: help site test guard

help:
	@echo "make site   assemble the deployable static site in _site/"
	@echo "make test   guard + integrations + reference backend suites (Python $(PYTHON), no dependencies)"
	@echo "make guard  just the public-repo guard (also runs first in make test)"

site:
	rm -rf _site
	mkdir -p _site/agent-prompts _site/integrations/hermes/caret-connect
	cp -R docs/. _site/
	cp openapi.yaml _site/openapi.yaml
	cp agent-prompts/*.md _site/agent-prompts/
	cp integrations/hermes/caret-connect/SKILL.md _site/integrations/hermes/caret-connect/SKILL.md
	$(PYTHON) scripts/public_guard.py

guard:
	$(PYTHON) scripts/public_guard.py

test: guard
	$(PYTHON) -m unittest discover -s scripts -p 'test_*.py'
	cd reference-backend && $(PYTHON) -m unittest discover -s tests
