.PHONY: docs manual manual-check

PYTHON ?= .venv/bin/python

install:
	pip install .

run:
	sailworks

build-standalone:
	nuitka3 sailworks/__main__.py --follow-imports --follow-stdlib

setup-macos:
	bash tools/macos/setup.sh

docs:
	make -C docs html

# Regenerate the manual screenshots from the running application, then check
# that the committed images still match their manifest.
manual:
	$(PYTHON) tools/capture_manual.py --all

manual-check:
	$(PYTHON) tools/capture_manual.py --check
