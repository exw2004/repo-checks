"""Checks shared by every repo, run pinned: uvx --from git+https://github.com/exw2004/repo-checks@<tag> …"""

from importlib.metadata import version

__version__ = version("repo-checks")  # one source: pyproject.toml
