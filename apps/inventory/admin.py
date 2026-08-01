from decimal import Decimal, InvalidOperation

from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Count, Max, Sum
from django.http import Http404, HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.translation import gettext_lazy as _
from import_export.admin import ExportActionModelAdmin, ImportMixin
from simple_history.admin import SimpleHistoryAdmin

from apps.core.permissions import can_see_costs

from .models import Category, Product, ProductImage, ProductVariant, StockMovement
from .resources import (
    ProductVariantImportResource,
    ProductVariantResource,
    StaffProductVariantResource,
)
from .services import add_movement, adjust_to_count


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ["name", "product_count"]
    search_fields = ["name"]

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(_product_count=Count("products"))

    @admin.display(description=_("products"), ordering="_product_count")
    def product_count(self, obj):
        return obj._product_count


class ProductImageInline(admin.TabularInline):
    model = ProductImage
    extra = 1
    # Just the image — no position field to fill in. Order is automatic (the
    # order you upload in, see ProductAdmin.save_formset); thumbnail is
    # derived on save (ProductImage.save -> _make_thumbnail), never hand-
    # uploaded — same discipline the old single `photo` field had.
    fields = ["image"]


class VariantInline(admin.TabularInline):
    model = ProductVariant
    extra = 0
    fields = [
        "sku",
        "size",
        "color",
        "currency",
        "cost_price",
        "sale_price",
        "low_stock_threshold",
        "is_active",
    ]

    def get_fields(self, request, obj=None):
        fields = super().get_fields(request, obj)
        if not can_see_costs(request.user):
            fields = [f for f in fields if f != "cost_price"]
        return fields


@admin.register(Product)
class ProductAdmin(SimpleHistoryAdmin, admin.ModelAdmin):
    list_display = ["name", "category", "current_stock", "is_active", "created_at"]
    list_filter = ["category", "is_active"]
    search_fields = ["name"]
    inlines = [ProductImageInline, VariantInline]
    list_select_related = ["category"]

    def get_queryset(self, request):
        # Stock per product = sum across its variants, annotated in SQL.
        return super().get_queryset(request).annotate(_stock=Sum("variants__movements__quantity"))

    @admin.display(description=_("stock"), ordering="_stock")
    def current_stock(self, obj):
        return obj._stock or 0

    def save_formset(self, request, form, formset, change):
        """ProductImageInline has no position field to fill in — newly added
        rows get sequentially increasing positions here, in the order they
        appear in the formset (upload order), continuing after whatever this
        product's highest existing position already is. Deliberately NOT in
        ProductImage.save() itself: that would also clobber an explicit
        position passed by the migration backfill or a test, which must stay
        controllable. Existing rows are left exactly as saved — this only
        ever assigns a position to a row that doesn't have one yet."""
        if formset.model is not ProductImage:
            formset.save()
            return
        instances = formset.save(commit=False)
        next_position = ProductImage.objects.filter(product=form.instance).aggregate(
            m=Max("position")
        )["m"]
        next_position = 0 if next_position is None else next_position + 1
        for obj in instances:
            if obj.pk is None:
                obj.position = next_position
                next_position += 1
            obj.save()
        for obj in formset.deleted_objects:
            obj.delete()
        formset.save_m2m()


def _parse_qty(raw: str) -> int | None:
    try:
        value = int(Decimal(raw.strip()))
    except (InvalidOperation, AttributeError, ValueError):
        return None
    return value if value > 0 else None


@admin.register(ProductVariant)
class ProductVariantAdmin(
    SimpleHistoryAdmin, ImportMixin, ExportActionModelAdmin, admin.ModelAdmin
):
    list_display = [
        "sku",
        "product",
        "size",
        "color",
        "current_stock",
        "sale_price",
        "currency",
        "is_active",
    ]
    list_filter = ["is_active", "product__category"]
    search_fields = ["sku", "product__name", "color"]
    search_help_text = "Артикул, название товара или цвет"
    actions = [
        "receive_stock_selected",
        "writeoff_selected",
        "recount_selected",
    ]

    def get_export_resource_classes(self, request):
        if can_see_costs(request.user):
            return [ProductVariantResource]
        return [StaffProductVariantResource]

    def get_import_resource_classes(self, request):
        return [ProductVariantImportResource]

    def has_import_permission(self, request):
        # Imports carry cost prices — superuser only.
        return can_see_costs(request.user)

    def get_queryset(self, request):
        # One query for the whole list page: join product, aggregate stock in SQL.
        qs = super().get_queryset(request).select_related("product")
        return qs.annotate(_stock=Sum("movements__quantity"))

    @admin.display(description=_("stock"), ordering="_stock")
    def current_stock(self, obj):
        return obj._stock or 0

    def get_list_display(self, request):
        cols = list(super().get_list_display(request))
        if can_see_costs(request.user):
            cols.insert(cols.index("sale_price"), "cost_price")
        return cols

    change_form_template = "admin/inventory/variant_change_form.html"

    def get_fields(self, request, obj=None):
        fields = super().get_fields(request, obj)
        if not can_see_costs(request.user):
            fields = [f for f in fields if f != "cost_price"]
        return fields

    # --- Per-variant stock panel on the change page: add / write off / set the
    # exact count, and read the full movement history with a running remainder.
    # Everything still writes append-only ledger rows through services — the
    # panel is a friendly face on the ledger, never a hand-edited quantity. ---

    def get_urls(self):
        custom = [
            path(
                "<int:pk>/stock-move/",
                self.admin_site.admin_view(self.stock_move_view),
                name="inventory_productvariant_stockmove",
            ),
        ]
        return custom + super().get_urls()

    def _variant_change_url(self, pk):
        return reverse("admin:inventory_productvariant_change", args=[pk])

    def stock_move_view(self, request, pk):
        """Handle a single-variant stock action posted from the change page.
        One endpoint, three operations (receive / write off / recount) chosen by
        the `op` field — each delegates to services, never touches the ledger by
        hand, and bounces back to the change page with a Russian result message."""
        variant = self.get_object(request, pk)
        if variant is None:
            raise Http404
        if not self.has_change_permission(request, variant):
            raise PermissionDenied
        back = HttpResponseRedirect(self._variant_change_url(pk))
        if request.method != "POST":
            return back

        op = request.POST.get("op")
        try:
            if op in ("receive", "writeoff"):
                qty = _parse_qty(request.POST.get("qty", ""))
                if not qty:
                    self.message_user(
                        request, _("Enter a positive quantity."), level=messages.ERROR
                    )
                elif op == "writeoff" and qty > variant.stock:
                    self.message_user(
                        request,
                        _("Cannot write off %(n)s — only %(s)s in stock.")
                        % {"n": qty, "s": variant.stock},
                        level=messages.ERROR,
                    )
                else:
                    mtype = (
                        StockMovement.PRODUCTION_IN
                        if op == "receive"
                        else StockMovement.WRITEOFF_OUT
                    )
                    label = _("Receive stock") if op == "receive" else _("Write off")
                    add_movement(variant, mtype, qty, user=request.user, reason=str(label))
                    self.message_user(
                        request,
                        _("Done — stock is now %(s)s.") % {"s": variant.stock},
                    )
            elif op == "recount":
                reason = request.POST.get("reason", "").strip()
                raw = request.POST.get("count", "").strip()
                try:
                    counted = int(raw)
                except (TypeError, ValueError):
                    counted = None
                if counted is None or counted < 0:
                    self.message_user(
                        request, _("Enter the counted quantity (0 or more)."), level=messages.ERROR
                    )
                elif not reason:
                    self.message_user(
                        request, _("A reason is required for a recount."), level=messages.ERROR
                    )
                else:
                    moved = adjust_to_count(variant, counted, request.user, reason)
                    if moved is None:
                        self.message_user(request, _("Count matched — nothing changed."))
                    else:
                        self.message_user(
                            request, _("Recounted — stock is now %(s)s.") % {"s": variant.stock}
                        )
        except (ValidationError, ValueError) as exc:
            self.message_user(
                request,
                "; ".join(exc.messages) if hasattr(exc, "messages") else str(exc),
                level=messages.ERROR,
            )
        return back

    def _stock_history(self, variant, limit=100):
        """Movement rows newest-first, each annotated with the running remainder
        AFTER that movement — so the column reads like a bank statement."""
        moves = list(
            variant.movements.select_related("created_by", "sale_order").order_by(
                "created_at", "pk"
            )
        )
        running = 0
        rows = []
        for m in moves:
            running += m.quantity
            rows.append({"move": m, "remainder": running})
        rows.reverse()
        return rows[:limit], running

    def change_view(self, request, object_id, form_url="", extra_context=None):
        extra_context = extra_context or {}
        variant = self.get_object(request, object_id)
        if variant is not None:
            history, current = self._stock_history(variant)
            extra_context.update(
                {
                    "stock_current": current,
                    "stock_history": history,
                    "stock_move_url": reverse(
                        "admin:inventory_productvariant_stockmove", args=[object_id]
                    ),
                    "stock_can_change": self.has_change_permission(request, variant),
                }
            )
        return super().change_view(request, object_id, form_url, extra_context)

    # --- Bulk stock buttons (multi-variant intake from the list): each asks for
    # a number (and a reason for recount) and writes the ledger row behind the
    # scenes via apps.inventory.services. ---

    def _intake_or_writeoff(self, request, queryset, movement_type, title, action_name):
        variants = list(queryset.select_related("product"))
        if request.POST.get("apply"):
            applied = 0
            for v in variants:
                qty = _parse_qty(request.POST.get(f"qty_{v.pk}", ""))
                if not qty:
                    continue
                add_movement(v, movement_type, qty, user=request.user, reason=str(title))
                applied += 1
            if applied:
                self.message_user(request, _("Updated stock for %(n)s item(s).") % {"n": applied})
            else:
                self.message_user(
                    request, _("No quantities entered — nothing changed."), level=messages.WARNING
                )
            return None

        context = {
            **self.admin_site.each_context(request),
            "title": title,
            "variants": variants,
            "selected": [str(v.pk) for v in variants],
            "action_name": action_name,
            "mode": "qty",
            "opts": self.model._meta,
        }
        return TemplateResponse(request, "admin/inventory/stock_action.html", context)

    @admin.action(description=_("Receive stock (Принять на склад)"))
    def receive_stock_selected(self, request, queryset):
        return self._intake_or_writeoff(
            request,
            queryset,
            StockMovement.PRODUCTION_IN,
            _("Receive stock"),
            "receive_stock_selected",
        )

    @admin.action(description=_("Write off (Списать)"))
    def writeoff_selected(self, request, queryset):
        return self._intake_or_writeoff(
            request, queryset, StockMovement.WRITEOFF_OUT, _("Write off stock"), "writeoff_selected"
        )

    @admin.action(description=_("Recount (Пересчёт)"))
    def recount_selected(self, request, queryset):
        variants = list(queryset.select_related("product"))
        if request.POST.get("apply"):
            reason = request.POST.get("reason", "").strip()
            if not reason:
                self.message_user(
                    request, _("A reason is required for a recount."), level=messages.ERROR
                )
            else:
                applied = 0
                for v in variants:
                    raw = request.POST.get(f"count_{v.pk}", "").strip()
                    if raw == "":
                        continue
                    try:
                        counted = int(raw)
                    except ValueError:
                        continue
                    adjust_to_count(v, counted, request.user, reason)
                    applied += 1
                self.message_user(request, _("Recounted %(n)s item(s).") % {"n": applied})
                return None

        context = {
            **self.admin_site.each_context(request),
            "title": _("Recount"),
            "variants": variants,
            "selected": [str(v.pk) for v in variants],
            "action_name": "recount_selected",
            "mode": "count",
            "opts": self.model._meta,
        }
        return TemplateResponse(request, "admin/inventory/stock_action.html", context)


@admin.register(StockMovement)
class StockMovementAdmin(admin.ModelAdmin):
    """The ledger is append-only and not in the sidebar — stock changes go
    through the Receive/Write off/Recount buttons on the variant list. This
    stays reachable by direct URL for a superuser auditing the raw log."""

    list_display = ["created_at", "variant", "movement_type", "quantity", "reason", "created_by"]
    list_filter = ["movement_type"]
    search_fields = ["variant__sku", "variant__product__name", "reason"]
    list_select_related = ["variant__product", "created_by"]
    fields = ["variant", "movement_type", "quantity", "reason"]
    autocomplete_fields = ["variant"]

    def has_module_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        qty = abs(obj.quantity)
        if obj.movement_type in StockMovement.OUT_TYPES:
            qty = -qty
        obj.quantity = qty
        obj.created_by = request.user
        obj.full_clean()
        super().save_model(request, obj, form, change)
