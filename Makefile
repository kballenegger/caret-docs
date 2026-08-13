# Validation entrypoints for this repository.
#
# Everything here is the standard library and nothing else — the same
# requirement the reference backend itself has. `make test` is the whole
# check; if it passes, the backend honours the contract in openapi.yaml.

PYTHON ?= python3

.PHONY: help site test

help:
	@echo "make site   assemble the deployable static site in _site/"
	@echo "make test   run the reference backend test suite (Python $(PYTHON), no dependencies)"

site:
	rm -rf _site
	mkdir -p _site/agent-prompts
	cp -R docs/. _site/
	cp openapi.yaml _site/openapi.yaml
	cp agent-prompts/*.md _site/agent-prompts/

test:
	cd reference-backend && $(PYTHON) -m unittest discover -s tests
