from decimal import Decimal, InvalidOperation

from django import forms
from django.contrib import admin, messages
from django.core.exceptions import ValidationError
from django.db.models import DecimalField, Sum, Value
from django.db.models.functions import Coalesce
from django.template.response import TemplateResponse
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from simple_history.admin import SimpleHistoryAdmin

from .models import Payment, SaleItem, SaleOrder
from .services import cancel_sale, confirm_sale, mark_fully_paid, record_payment, void_payment

_BADGE_COLORS = {
    SaleOrder.PAID: "#1a7f37",
    SaleOrder.PARTIAL: "#9a6700",
    SaleOrder.UNPAID: "#b42318",
}
_BADGE_LABELS = {
    SaleOrder.PAID: _("Paid"),
    SaleOrder.PARTIAL: _("Partial"),
    SaleOrder.UNPAID: _("Unpaid"),
}


class SaleItemInline(admin.TabularInline):
    model = SaleItem
    extra = 0
    autocomplete_fields = ["variant"]


class PaymentInlineForm(forms.ModelForm):
    """Two things live here that plain model validation can't express:
    - a payment that would overpay the order needs an explicit tick, not a
      silent accept (the "much larger than the total" guardrail);
    - once a payment is reviewed, only a superuser may still edit it — enforced
      by disabling the fields, since Django inlines can't gate permissions
      per-row (has_change_permission only sees the parent SaleOrder).
    """

    confirm_overpayment = forms.BooleanField(
        required=False,
        label=_("confirm overpayment"),
        help_text=_("Tick to allow a payment larger than the order's remaining balance."),
    )

    class Meta:
        model = Payment
        fields = ["client", "amount", "currency", "method", "note"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        user = getattr(self, "_request_user", None)
        locked = self.instance.pk and self.instance.reviewed and user and not user.is_superuser
        if locked:
            for name in ["client", "amount", "currency", "method", "note"]:
                self.fields[name].disabled = True
            self.fields["confirm_overpayment"].disabled = True


class PaymentInlineFormSet(forms.BaseInlineFormSet):
    def clean(self):
        super().clean()
        order = self.instance
        if not order.pk or order.status != SaleOrder.CONFIRMED:
            return
        running_total = order.paid_amount  # already-saved payments in this order's currency
        for form in self.forms:
            if not hasattr(form, "cleaned_data") or form.cleaned_data.get("DELETE"):
                continue
            if form.instance.pk:
                continue  # existing rows are already counted in paid_amount
            amount = form.cleaned_data.get("amount") or Decimal("0")
            currency = form.cleaned_data.get("currency") or order.currency
            if currency != order.currency or amount <= 0:
                continue
            running_total += amount
            if running_total > order.total and not form.cleaned_data.get("confirm_overpayment"):
                form.add_error(
                    "amount",
                    _(
                        "This would overpay the order (total %(total)s, already paid "
                        "%(paid)s). Tick 'confirm overpayment' to proceed anyway."
                    )
                    % {"total": order.total, "paid": order.paid_amount},
                )


class PaymentInline(admin.TabularInline):
    model = Payment
    form = PaymentInlineForm
    formset = PaymentInlineFormSet
    extra = 0
    can_delete = False  # never delete a payment — void it (creates a reversing entry)
    fields = [
        "client",
        "amount",
        "currency",
        "method",
        "note",
        "reviewed",
        "created_by",
        "created_at",
    ]
    readonly_fields = ["reviewed", "created_by", "created_at"]
    autocomplete_fields = ["client"]
    verbose_name_plural = _("payments (for loans / partial payment)")

    def get_formset(self, request, obj=None, **kwargs):
        formset = super().get_formset(request, obj, **kwargs)
        bound_form = type(
            "BoundPaymentInlineForm", (formset.form,), {"_request_user": request.user}
        )
        formset.form = bound_form
        return formset


@admin.register(SaleOrder)
class SaleOrderAdmin(SimpleHistoryAdmin, admin.ModelAdmin):
    list_display = [
        "id",
        "client",
        "channel",
        "status",
        "total_display",
        "paid_column",
        "balance_column",
        "payment_badge",
        "created_at",
        "confirmed_at",
    ]
    list_filter = ["status", "channel", "currency"]
    search_fields = ["id", "client__name", "client__phone"]
    list_select_related = ["client"]
    autocomplete_fields = ["client"]
    readonly_fields = ["total", "status", "confirmed_at", "created_by"]
    inlines = [SaleItemInline, PaymentInline]
    actions = ["approve_selected", "mark_paid_selected", "cancel_selected"]

    def get_queryset(self, request):
        # Sum same-currency payments per order in SQL (one aggregate joined to a
        # stored total — no row multiplication) so the payment badge stays
        # single-query.
        from django.db.models import F, Q

        return (
            super()
            .get_queryset(request)
            .annotate(
                _paid=Coalesce(
                    Sum("payments__amount", filter=Q(payments__currency=F("currency"))),
                    Value(Decimal("0")),
                    output_field=DecimalField(max_digits=14, decimal_places=2),
                )
            )
        )

    @admin.display(description=_("total"), ordering="total")
    def total_display(self, obj):
        return f"{obj.total} {obj.currency}"

    @admin.display(description=_("paid"), ordering="_paid")
    def paid_column(self, obj):
        if obj.status != SaleOrder.CONFIRMED or obj.client_id is None:
            return "—"
        return f"{obj._paid} {obj.currency}"

    @admin.display(description=_("balance"))
    def balance_column(self, obj):
        if obj.status != SaleOrder.CONFIRMED or obj.client_id is None:
            return "—"
        return f"{max(obj.total - obj._paid, Decimal('0'))} {obj.currency}"

    @admin.display(description=_("payment"))
    def payment_badge(self, obj):
        # Only approved sales that have a client carry a tracked balance; pending,
        # cancelled, and walk-in (paid-on-the-spot) orders show nothing.
        if obj.status != SaleOrder.CONFIRMED or obj.client_id is None:
            return "—"
        status = SaleOrder.payment_status_for(obj.total, obj._paid)
        return format_html(
            '<span style="background:{};color:#fff;padding:2px 8px;'
            'border-radius:10px;font-size:11px;white-space:nowrap">{}</span>',
            _BADGE_COLORS[status],
            _BADGE_LABELS[status],
        )

    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)

    def save_formset(self, request, form, formset, change):
        # Stamp created_by on inline payments, and default their client to the
        # order's client so a partial payment/loan needs one less field.
        instances = formset.save(commit=False)
        for obj in instances:
            if isinstance(obj, Payment):
                if obj.client_id is None and form.instance.client_id:
                    obj.client = form.instance.client
                if obj.created_by_id is None:
                    obj.created_by = request.user
            obj.save()
        formset.save_m2m()
        for obj in formset.deleted_objects:
            obj.delete()

    @admin.action(description=_("Approve selected sales — record payment"))
    def approve_selected(self, request, queryset):
        """Two-step: first click shows a small form (Paid in full / Partial /
        Loan); confirming writes off stock and auto-creates the payment."""
        pending = list(queryset.filter(status=SaleOrder.DRAFT).select_related("client"))

        if request.POST.get("apply"):
            choice = request.POST.get("pay", "loan")
            amount = self._parse_amount(request.POST.get("amount", ""))
            approved = 0
            for order in pending:
                try:
                    confirm_sale(order, user=request.user)
                except ValidationError as exc:
                    self.message_user(
                        request, f"#{order.pk}: " + "; ".join(exc.messages), level=messages.ERROR
                    )
                    continue
                approved += 1
                if choice == "full":
                    record_payment(order, order.total, user=request.user)
                elif choice == "partial" and amount:
                    record_payment(order, min(amount, order.total), user=request.user)
            if approved:
                self.message_user(request, _("Approved %(n)s sale(s).") % {"n": approved})
            return None

        if not pending:
            self.message_user(
                request, _("None of the selected sales are pending."), level=messages.WARNING
            )
            return None

        context = {
            **self.admin_site.each_context(request),
            "title": _("Approve sales"),
            "orders": pending,
            "selected": [str(o.pk) for o in pending],
            "opts": self.model._meta,
        }
        return TemplateResponse(request, "admin/sales/approve_sales.html", context)

    @staticmethod
    def _parse_amount(raw):
        try:
            value = Decimal(raw.strip())
        except (InvalidOperation, AttributeError):
            return None
        return value if value > 0 else None

    @admin.action(description=_("Mark fully paid (settle balance)"))
    def mark_paid_selected(self, request, queryset):
        settled = 0
        for order in queryset:
            try:
                if mark_fully_paid(order, user=request.user):
                    settled += 1
            except ValidationError as exc:
                self.message_user(
                    request, f"#{order.pk}: " + "; ".join(exc.messages), level=messages.ERROR
                )
        self.message_user(request, _("Settled %(n)s order(s).") % {"n": settled})

    @admin.action(description=_("Cancel selected sales (returns stock)"))
    def cancel_selected(self, request, queryset):
        for order in queryset:
            try:
                cancel_sale(order, user=request.user)
                self.message_user(request, _("Sale #%(id)s cancelled.") % {"id": order.pk})
            except ValidationError as exc:
                self.message_user(request, "; ".join(exc.messages), level=messages.ERROR)


@admin.register(Payment)
class PaymentAdmin(SimpleHistoryAdmin, admin.ModelAdmin):
    """Never a standalone sidebar entry (payments live inside their sale) —
    reachable directly for the rare operations that need it: voiding a payment,
    or a superuser auditing raw payment history."""

    list_display = [
        "created_at",
        "client",
        "amount",
        "currency",
        "method",
        "order",
        "reviewed",
        "created_by",
    ]
    list_filter = ["method", "currency", "reviewed"]
    search_fields = ["client__name", "client__phone"]
    list_select_related = ["client", "order", "created_by"]
    autocomplete_fields = ["client", "order"]
    actions = ["void_selected"]

    def has_module_permission(self, request):
        return False

    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)

    @admin.action(description=_("Void selected payments (creates a reversing entry)"))
    def void_selected(self, request, queryset):
        voided = 0
        for payment in queryset:
            try:
                void_payment(payment, user=request.user)
                voided += 1
            except ValidationError as exc:
                self.message_user(
                    request, f"#{payment.pk}: " + "; ".join(exc.messages), level=messages.ERROR
                )
        self.message_user(request, _("Voided %(n)s payment(s).") % {"n": voided})
