DATE = $(shell date +'%Y%m%d')

build:
	python -m build --wheel
	rm -rf build

install:
	pip uninstall fbn -y
	pip install dist/*.whl

install-dev:
	pip uninstall fbn -y
	pip install -e .
	pip install -r requirements-dev.txt

smoketest:
	fbn --help
	fbn --version

clean:
	rm -rf dist build
	rm -rf *.egg-info
	find . -name \*.pyc -delete