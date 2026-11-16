"""Defaults for existing suites: they register several users without invite codes.

Production still defaults to closed registration and a rate limit of 10.
Hardening tests override these on config at request time.
"""
import os

os.environ.setdefault("TB_OPEN_REGISTRATION", "1")
os.environ.setdefault("TB_AUTH_RATE_LIMIT", "0")
# Background learning makes extra model calls after turns; suites that count calls keep it off.
# tests/test_learning.py switches it on where it is tested.
os.environ.setdefault("TB_LEARNING", "0")
