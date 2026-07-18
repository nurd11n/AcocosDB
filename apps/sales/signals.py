"""Invalidate the dashboard/catalog cache when money moves.

A sale confirm/cancel/return already bumps the catalog version through its
StockMovement writes (see apps/inventory/signals.py), so the dashboard cache —
keyed on the catalog version — refreshes for those. A Payment, however, changes
debt without touching stock, so it needs its own bump; otherwise the Долги panel
could show a stale figure for up to the 5-minute TTL.
"""

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from apps.inventory.cache import bump_catalog_version

from .models import Payment


@receiver(post_save, sender=Payment)
@receiver(post_delete, sender=Payment)
def _invalidate_on_payment(sender, **kwargs):
    bump_catalog_version()
