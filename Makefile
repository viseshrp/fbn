build:
	python -m build

install:
	pip uninstall fbn -y
	pip install --find-links dist fbn

install-dev:
	pip install -e ".[dev]"

test:
	python -m pytest

lint:
	python -m ruff check .
	python -m ruff format --check .

smoketest:
	fbn --help
	fbn --version

clean:
	rm -rf build dist .pytest_cache .ruff_cache htmlcov
	rm -rf *.egg-info
	find . -name \*.pyc -delete
