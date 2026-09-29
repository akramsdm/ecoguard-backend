"""Root pytest configuration.

``smoke_test.py`` at the repo root matches pytest's ``*_test.py`` collection
pattern. Importing it as a test module runs its module-level code, which
overwrites ``APP_ENV`` with ``development`` — silently enabling the live auth
rate limiter and making test runs flaky (429 responses near the end of a full
run). It is a manual smoke-check script, not a pytest module, so exclude it
from collection here.
"""
collect_ignore = ['smoke_test.py']