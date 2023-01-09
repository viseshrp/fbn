"""The setup script."""
import os
from io import open

from setuptools import setup, find_packages

REQUIREMENTS = [
    "click>=8.1.3",
    "facebook-scraper>=0.2.59",
    "tenacity>=8.1.0",
    "schedule>=1.1.0",
    "apprise>=1.2.1",
]

curr_dir = os.path.abspath(os.path.dirname(__file__))


def get_file_text(file_name):
    with open(os.path.join(curr_dir, file_name), "r", encoding="utf-8") as in_file:
        return in_file.read()


_version = {}
_version_file = os.path.join(curr_dir, "fbn", "__init__.py")
with open(_version_file) as fp:
    exec(fp.read(), _version)
version = _version["__version__"]

setup(
    name="fbn",
    version=version,
    description="Tool to monitor fb groups and notify",
    long_description=get_file_text("README.md")
    + "\n\n"
    + get_file_text("CHANGELOG.md"),
    long_description_content_type="text/markdown",
    author="Visesh Prasad",
    author_email="visesh@live.com",
    maintainer="Visesh Prasad",
    maintainer_email="visesh@live.com",
    license="MIT license",
    packages=find_packages(include=["fbn"]),
    include_package_data=True,
    zip_safe=False,
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
        "Programming Language :: Python :: 3.7",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Topic :: Software Development :: Libraries :: Python Modules",
    ],
    url="https://github.com/viseshrp/fbn",
    project_urls={
        "Documentation": "https://github.com/viseshrp/fbn",
        "Changelog": "https://github.com/viseshrp/fbn/blob/main/CHANGELOG.md",
        "Bug Tracker": "https://github.com/viseshrp/fbn/issues",
        "Source Code": "https://github.com/viseshrp/fbn",
    },
    python_requires=">=3.7",
    keywords="fb fbn facebook group notify monitor",
    test_suite="tests",
    install_requires=REQUIREMENTS,
    entry_points={
        "console_scripts": [
            "fbn=fbn.__main__:main",
        ],
    },
)
