from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _
from simple_history.models import HistoricalRecords


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
    photo = models.ImageField(_("photo"), upload_to="products/", blank=True)
    is_active = models.BooleanField(_("active"), default=True)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    history = HistoricalRecords()

    class Meta:
        verbose_name = _("product")
        verbose_name_plural = _("products")
        ordering = ["name"]

    def __str__(self):
        return self.name


class ProductVariant(models.Model):
    product = models.ForeignKey(
        Product, verbose_name=_("product"), on_delete=models.PROTECT, related_name="variants"
    )
    sku = models.CharField("SKU", max_length=64, unique=True)
    size = models.CharField(_("size"), max_length=32, blank=True)
    color = models.CharField(_("color"), max_length=64, blank=True)
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
    IN_TYPES = {PRODUCTION_IN, PURCHASE_IN, RETURN_IN}
    OUT_TYPES = {SALE_OUT, WRITEOFF_OUT}

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
        indexes = [models.Index(fields=["variant", "movement_type"])]

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
