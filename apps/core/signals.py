"""Clear the cached day-rates when an ExchangeRate is written, so an owner
override (or the daily fetch) shows in «≈ сом» figures immediately instead of
waiting out the cache TTL."""

from django.core.cache import cache
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .models import ExchangeRate


@receiver(post_save, sender=ExchangeRate)
@receiver(post_delete, sender=ExchangeRate)
def _clear_rate_cache(sender, instance, **kwargs):
    cache.delete(f"rates:{instance.date.isoformat()}")
