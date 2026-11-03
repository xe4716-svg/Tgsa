"""Canonical namespace for the preserved business implementation.

Use the existing source directly. Do not import its standalone HTTP/auth app
into the root API or create another copy of its database or queue implementation.
"""
from pathlib import Path

__path__ = [str(Path(__file__).resolve().parents[2] / 'workstreams' / 'tgmate-v2' / 'app')]
