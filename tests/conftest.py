"""Defaults for existing suites: they register several users without invite codes.

Production still defaults to closed registration and a rate limit of 10.
Hardening tests override these on config at request time.
"""
import os

os.environ.setdefault("TB_OPEN_REGISTRATION", "1")
os.environ.setdefault("TB_AUTH_RATE_LIMIT", "0")
