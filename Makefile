# Validation entrypoints for this repository.
#
# Everything here is the standard library and nothing else — the same
# requirement the reference backend itself has. `make test` is the whole
# check; if it passes, the backend honours the contract in openapi.yaml.

PYTHON ?= python3

.PHONY: help test

help:
	@echo "make test   run the reference backend test suite (Python $(PYTHON), no dependencies)"

test:
	cd reference-backend && $(PYTHON) -m unittest discover -s tests
