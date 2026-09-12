"""Tests for the Debian AI Assistant spine.

Run them with either::

    make test                       # or
    python3 -m unittest discover -s tests -t .

The suite is stdlib-only (like the spine itself) and needs no network: the
services are exercised in-process against a stub OpenAI-compatible upstream.
"""
