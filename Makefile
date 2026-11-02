.PHONY: docs

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
