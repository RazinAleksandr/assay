"""Assay — the quality gate for the running app."""
from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("assay")
except PackageNotFoundError:   # a source checkout that is not installed
    __version__ = "0.0.0"
