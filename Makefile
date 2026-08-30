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

.PHONY: help site test guard docs spec

help:
	@echo "make site   assemble the deployable static site in _site/"
	@echo "make test   guard + docs check + spec check + script tests + archived backend suite (Python $(PYTHON), no dependencies)"
	@echo "make guard  just the public-repo guard (also runs first in make test)"
	@echo "make docs   just the docs structure guard"
	@echo "make spec   regenerate the derived cleanup-spec files in spec/cleanup/v1/"

# The extra copies below (openapi.yaml, agent-prompts/, integrations/)
# keep pre-V4 deep links resolving at their old URLs; every copied file
# is banner-marked as archived. scripts/docs_check.py mirrors this
# layout in EXTRA_SITE_FILES — change both together.
site:
	rm -rf _site
	mkdir -p _site/spec/cleanup/v1 \
	         _site/agent-prompts \
	         _site/integrations/hermes/caret-connect \
	         _site/legacy/agent-prompts \
	         _site/legacy/integrations/hermes/caret-connect
	cp -R docs/. _site/
	cp spec/cleanup/v1/* _site/spec/cleanup/v1/
	cp legacy/openapi.yaml _site/legacy/openapi.yaml
	cp legacy/openapi.yaml _site/openapi.yaml
	cp legacy/agent-prompts/*.md _site/legacy/agent-prompts/
	cp legacy/agent-prompts/*.md _site/agent-prompts/
	cp legacy/integrations/hermes/caret-connect/SKILL.md _site/legacy/integrations/hermes/caret-connect/SKILL.md
	cp legacy/integrations/hermes/caret-connect/SKILL.md _site/integrations/hermes/caret-connect/SKILL.md
	$(PYTHON) scripts/public_guard.py
	$(PYTHON) scripts/docs_check.py

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
	cd legacy/reference-backend && $(PYTHON) -m unittest discover -s tests
