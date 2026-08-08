"""Shared pytest fixtures.

Cache state is NOT part of the DB transaction pytest-django rolls back after
each test — only the database is. Every test client request in this suite
comes from the same IP (127.0.0.1), so a per-IP cache-based rate limiter
(apps.core.ratelimit) accumulates its counter ACROSS test functions unless
the cache is cleared between them: test A makes 2 requests, test B makes 3,
test C is the one that happens to trip the limit at request #11 even though
no single test came close — a real failure this exact way is what this
fixture fixes. autouse so every test gets a clean cache without needing to
remember to ask for it.
"""

import pytest
from django.core.cache import cache


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()
