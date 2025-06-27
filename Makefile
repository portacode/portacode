# Makefile for Python package release automation

.PHONY: release

release:
	@echo "Usage: make release VERSION=x.y.z [or just 'make release' for date-based version]"

release-auto:
	$(MAKE) release VERSION=$(shell date +%Y.%m.%d.%H%M%S)

release-real:
ifndef VERSION
	$(MAKE) release-auto
else
	rm -rf dist/*
	# Ensure we're on a clean state
	git pull
	# Create and push the tag
	git tag -f v$(VERSION)
	git push -f origin v$(VERSION)
	# Clean any previous builds
	rm -rf build/ *.egg-info
	# Build from the tagged state
	SETUPTOOLS_SCM_PRETEND_VERSION=$(VERSION) python -m build
	twine upload dist/* --config-file .pypirc
endif

# Default target
release: release-real 