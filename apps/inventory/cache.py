"""Catalog cache versioning.

The product grid (names, photos, prices, category, stock badge) is cacheable,
but it must go stale the instant stock or the catalog changes. Rather than track
individual keys, we embed a single monotonic version number in every catalog
cache key and bump it on any relevant write (see signals.py). A bump makes every
old key unreachable at once — cheap and always correct.

This NEVER gates whether a sale can happen: confirm_sale reads stock fresh from
the DB under a row lock. The worst a stale grid can do is show an out-of-date
badge; the confirm still fails honestly with «не хватает остатка».
"""

from django.core.cache import cache

CATALOG_VERSION_KEY = "catalog:v"


def catalog_version() -> int:
    version = cache.get(CATALOG_VERSION_KEY)
    if version is None:
        cache.set(CATALOG_VERSION_KEY, 1, None)  # persist with no expiry
        return 1
    return version


def bump_catalog_version() -> None:
    """Invalidate every catalog cache key at once.

    add-then-incr, not bare incr: if the key is absent — Redis restarted, or it
    was evicted — cache.incr() raises ValueError, which on the product-save
    signal path would bubble up and break the save. add() seeds the key only
    when missing (and seeding is itself a fresh version, so it invalidates old
    keys); when the key exists, incr() bumps it atomically. The except is the
    razor-thin race where the key expires between add() and incr()."""
    if cache.add(CATALOG_VERSION_KEY, 1, None):
        return  # key was absent — seeding to 1 already invalidates old versions
    try:
        cache.incr(CATALOG_VERSION_KEY)
    except ValueError:
        cache.set(CATALOG_VERSION_KEY, 1, None)
