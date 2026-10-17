.PHONY: docs

install:
	pip install .

run:
	gweatherrouting

build-standalone:
	nuitka3 gweatherrouting/__main__.py --follow-imports --follow-stdlib 

setup-macos:
	bash tools/macos/setup.sh

docs:
	make -C docs html
