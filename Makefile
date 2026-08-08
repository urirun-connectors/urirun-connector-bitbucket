.PHONY: test build

test:
	python -m pytest -q

build:
	python -m build --wheel
