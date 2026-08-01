from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _
from simple_history.models import HistoricalRecords

from apps.core.currency import CURRENCY_CHOICES

# Movement-type groupings, at module level so the model's CheckConstraints (in a
# nested Meta, which can't see class-body names) share the same source of truth
# as the IN_TYPES/OUT_TYPES sets used in Python.
INTAKE_TYPES = ("production_in", "purchase_in", "return_in")
OUTGOING_TYPES = ("sale_out", "writeoff_out")


class Category(models.Model):
    name = models.CharField(_("name"), max_length=120, unique=True)

    class Meta:
        verbose_name = _("category")
        verbose_name_plural = _("categories")
        ordering = ["name"]

    def __str__(self):
        return self.name


class Product(models.Model):
    category = models.ForeignKey(
        Category, verbose_name=_("category"), on_delete=models.PROTECT, related_name="products"
    )
    name = models.CharField(_("name"), max_length=200)
    description = models.TextField(_("description"), blank=True)
    is_active = models.BooleanField(_("active"), default=True)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    history = HistoricalRecords()

    class Meta:
        verbose_name = _("product")
        verbose_name_plural = _("products")
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def grid_image(self):
        """The small image a tile/catalog reply should show — the COVER
        (first-by-position ProductImage's thumbnail, or its original before a
        thumbnail exists), or None with nothing uploaded. `self.images.all()`
        rather than `.first()` on purpose: `.first()` builds a fresh queryset
        and ignores an existing prefetch_related("images") cache, which is
        exactly what the grid/catalog builders rely on to stay O(1) queries
        regardless of row count (see apps.pos.views._build_grid_tiles and its
        query-budget test) — a single-product caller with no prefetch (the
        Telegram bot, one product per message) still gets a normal lazy
        query, just not a cached one."""
        cover = next(iter(self.images.all()), None)
        if cover is None:
            return None
        return cover.thumbnail or cover.image


class ProductImage(models.Model):
    """One photo in a product's gallery — a product can have as many as a
    manager wants (CLAUDE.md previously only allowed one; this is the fix).
    `position` decides display order; the lowest position (ties broken by
    insertion order via `id`) is the COVER shown on tiles, in the client bot,
    and as a campaign's hero image — see Product.grid_image, the one place
    that decision is made, so every consumer agrees on what "the" photo is.

    position is NOT part of the admin form (see ProductImageInline) — a
    manager just uploads photos, in the order they want them to show, and
    ProductAdmin.save_formset assigns sequentially increasing positions to
    each new row automatically. It stays an editable model field for
    programmatic use (the gallery-backfill migration, tests) rather than a
    fixed/derived value, in case reordering ever gets its own UI later."""

    product = models.ForeignKey(
        Product, verbose_name=_("product"), on_delete=models.CASCADE, related_name="images"
    )
    image = models.ImageField(_("image"), upload_to="products/")
    # A ~400px JPEG derived from `image` on save — a grid tile ships this, not
    # the original (a 4MB phone photo × dozens of tiles would wreck load time).
    thumbnail = models.ImageField(
        _("thumbnail"), upload_to="products/thumbs/", blank=True, editable=False
    )
    position = models.PositiveIntegerField(_("position"), default=0)
    created_at = models.DateTimeField(_("uploaded at"), auto_now_add=True)

    class Meta:
        verbose_name = _("product image")
        verbose_name_plural = _("product images")
        ordering = ["position", "id"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._orig_image_name = self.image.name if self.image else None

    def __str__(self):
        return f"{self.product} · {self.position}"

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)  # write the image to disk first (need a pk for the filename)
        current = self.image.name if self.image else None
        if current != self._orig_image_name or not self.thumbnail:
            self._make_thumbnail()
            super().save(update_fields=["thumbnail"])
        self._orig_image_name = current

    def _make_thumbnail(self):
        from io import BytesIO

        from django.core.files.base import ContentFile
        from PIL import Image

        self.image.open()
        img = Image.open(self.image).convert("RGB")
        img.thumbnail((400, 400))  # max box, aspect preserved
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=82, optimize=True)
        self.thumbnail.save(f"thumb_{self.pk}.jpg", ContentFile(buf.getvalue()), save=False)


class ProductVariant(models.Model):
    product = models.ForeignKey(
        Product, verbose_name=_("product"), on_delete=models.PROTECT, related_name="variants"
    )
    sku = models.CharField("SKU", max_length=64, unique=True)
    size = models.CharField(_("size"), max_length=32, blank=True)
    color = models.CharField(_("color"), max_length=64, blank=True)
    currency = models.CharField(
        _("currency"),
        max_length=3,
        choices=CURRENCY_CHOICES,
        default=settings.CURRENCY,
        help_text=_("Applies to both cost price and sale price."),
    )
    cost_price = models.DecimalField(_("cost price"), max_digits=12, decimal_places=2)
    sale_price = models.DecimalField(_("sale price"), max_digits=12, decimal_places=2)
    low_stock_threshold = models.PositiveIntegerField(_("low stock threshold"), default=2)
    is_active = models.BooleanField(_("active"), default=True)
    history = HistoricalRecords()

    class Meta:
        verbose_name = _("product variant")
        verbose_name_plural = _("product variants")
        ordering = ["product__name", "size", "color"]

    def __str__(self):
        parts = [self.product.name, self.size, self.color]
        return " / ".join(p for p in parts if p)

    @property
    def stock(self) -> int:
        # Prefer a queryset-annotated `_stock` (list views, bot replies) so
        # iterating N variants costs 1 query, not N. The per-row aggregate is
        # the fallback for a single loose instance.
        annotated = getattr(self, "_stock", None)
        if annotated is not None:
            return annotated
        agg = self.movements.aggregate(s=models.Sum("quantity"))
        return agg["s"] or 0


class StockMovement(models.Model):
    """Immutable stock ledger. Current stock = SUM(quantity) per variant.

    `quantity` is signed: intake types are positive, outgoing types negative.
    Movements are only created (through services or the admin form), never edited.
    """

    PRODUCTION_IN = "production_in"
    PURCHASE_IN = "purchase_in"
    RETURN_IN = "return_in"
    SALE_OUT = "sale_out"
    WRITEOFF_OUT = "writeoff_out"
    ADJUSTMENT = "adjustment"

    TYPE_CHOICES = [
        (PRODUCTION_IN, _("Production intake")),
        (PURCHASE_IN, _("Purchase intake")),
        (RETURN_IN, _("Return")),
        (SALE_OUT, _("Sale")),
        (WRITEOFF_OUT, _("Write-off")),
        (ADJUSTMENT, _("Adjustment")),
    ]
    IN_TYPES = set(INTAKE_TYPES)
    OUT_TYPES = set(OUTGOING_TYPES)

    variant = models.ForeignKey(
        ProductVariant,
        verbose_name=_("product variant"),
        on_delete=models.PROTECT,
        related_name="movements",
    )
    movement_type = models.CharField(_("type"), max_length=20, choices=TYPE_CHOICES)
    quantity = models.IntegerField(_("quantity"))
    reason = models.CharField(_("reason"), max_length=255, blank=True)
    sale_order = models.ForeignKey(
        "sales.SaleOrder",
        verbose_name=_("sale order"),
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="movements",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("created by"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    class Meta:
        verbose_name = _("stock movement")
        verbose_name_plural = _("stock movements")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["variant", "movement_type"]),
            # The ledger-sum-per-variant and daily-report queries scan by
            # variant over a time range; clean() alone can't help the planner.
            models.Index(fields=["variant", "created_at"]),
        ]
        # DB-level integrity — clean() is bypassed by bulk_create/update, these
        # constraints are not. ADJUSTMENT may be either sign (reason required in
        # clean()), so it's exempt from the sign rules but still can't be zero.
        constraints = [
            models.CheckConstraint(condition=~Q(quantity=0), name="stockmovement_quantity_nonzero"),
            models.CheckConstraint(
                condition=~Q(movement_type__in=INTAKE_TYPES) | Q(quantity__gt=0),
                name="stockmovement_intake_positive",
            ),
            models.CheckConstraint(
                condition=~Q(movement_type__in=OUTGOING_TYPES) | Q(quantity__lt=0),
                name="stockmovement_outgoing_negative",
            ),
        ]

    def __str__(self):
        return f"{self.get_movement_type_display()} {self.quantity:+d} — {self.variant}"

    def clean(self):
        if self.quantity == 0:
            raise ValidationError(_("Quantity cannot be zero."))
        if self.movement_type == self.ADJUSTMENT and not self.reason:
            raise ValidationError(_("An adjustment requires a reason."))
        if self.movement_type in self.IN_TYPES and self.quantity < 0:
            raise ValidationError(_("Intake movements must have a positive quantity."))
        if self.movement_type in self.OUT_TYPES and self.quantity > 0:
            raise ValidationError(_("Outgoing movements must have a negative quantity."))
