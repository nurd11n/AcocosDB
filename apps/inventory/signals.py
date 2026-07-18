"""Bump the catalog cache version on any write that changes what the product
grid shows: a Product/ProductVariant edit (name, price, photo, active) or a
StockMovement (the stock badge). Wired in InventoryConfig.ready()."""

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .cache import bump_catalog_version
from .models import Product, ProductVariant, StockMovement


@receiver(post_save, sender=Product)
@receiver(post_delete, sender=Product)
@receiver(post_save, sender=ProductVariant)
@receiver(post_delete, sender=ProductVariant)
@receiver(post_save, sender=StockMovement)
@receiver(post_delete, sender=StockMovement)
def _invalidate_catalog(sender, **kwargs):
    bump_catalog_version()
