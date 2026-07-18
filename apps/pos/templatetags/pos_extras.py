from django import template
from django.utils.translation import gettext_lazy as _

from apps.clients.services import client_debt
from apps.sales.models import SaleOrder

register = template.Library()

# payment_status_for() returns "unpaid"/"partial"/"paid" — the CSS classes
# and money-bar tokens use "debt" for the unpaid case (see POS-DESIGN.md's
# --debt/--partial/--paid palette), and the label is the Russian status word.
_STATUS_CSS = {SaleOrder.UNPAID: "debt", SaleOrder.PARTIAL: "partial", SaleOrder.PAID: "paid"}
_STATUS_LABEL = {
    SaleOrder.UNPAID: _("долг"),
    SaleOrder.PARTIAL: _("частично"),
    SaleOrder.PAID: _("оплачено"),
}


@register.filter(name="client_debts")
def client_debts_filter(client):
    """{{ order.client|client_debts }} -> {currency: amount}. Used by the
    client chip on the sale screen — the debt must be visible right where a
    manager decides whether to sell more on credit (see POS-DESIGN.md)."""
    return client_debt(client) if client else {}


@register.filter(name="status_css")
def status_css_filter(status):
    return _STATUS_CSS.get(status, "")


@register.filter(name="status_label")
def status_label_filter(status):
    return _STATUS_LABEL.get(status, "")
