"""Undo/redo for the draft cart.

The basket lives on the server (a draft SaleOrder, so a reload never loses it —
CLAUDE.md), which means undo has to live there too: a client-side stack would
be wiped by the very reload the draft exists to survive.

Each mutating cart action pushes a snapshot of the state BEFORE it, into a
per-order stack in the session. Undo restores the top snapshot and pushes the
current state onto the redo stack; redo does the reverse.

SCOPE — deliberately the draft only. Nothing here can touch a confirmed sale, a
payment, or a stock movement: those reverse through services.cancel_sale /
return_items / void_payment, which write reversing ledger entries and keep the
audit trail intact. Undo is for "I tapped the wrong dress", not for rewriting
money that has already been counted.

Snapshots are plain JSON (Decimals as strings) because Django's default session
serializer is JSON, not pickle.
"""

from decimal import Decimal

from django.db import transaction

from apps.sales.models import SaleItem, SaleOrder

SESSION_KEY = "cart_undo"
# Deep enough to walk back through a whole miskeyed basket, shallow enough that
# the session cookie/row stays small.
STACK_LIMIT = 25
# A manager works one sale at a time; keeping a couple of recent drafts covers
# switching between two tabs without letting the session grow without bound.
TRACKED_ORDERS = 3


def _snapshot(order: SaleOrder) -> dict:
    return {
        "client_id": order.client_id,
        "items": [
            {
                "variant_id": item.variant_id,
                "quantity": item.quantity,
                "unit_price": str(item.unit_price),
                "discount_type": item.discount_type,
                "discount_value": str(item.discount_value),
            }
            for item in order.items.all().order_by("pk")
        ],
    }


def _stacks(session, order_pk: int) -> dict:
    store = session.setdefault(SESSION_KEY, {})
    key = str(order_pk)
    if key not in store:
        # Prune oldest tracked drafts first — dicts keep insertion order.
        while len(store) >= TRACKED_ORDERS:
            del store[next(iter(store))]
        store[key] = {"undo": [], "redo": []}
    return store[key]


def push(request, order: SaleOrder) -> None:
    """Record the state BEFORE a mutation. Call this at the top of every view
    that changes the cart, before it writes anything."""
    stacks = _stacks(request.session, order.pk)
    snap = _snapshot(order)
    if stacks["undo"] and stacks["undo"][-1] == snap:
        return  # a no-op action (e.g. an add that clamped to 0) adds no step
    stacks["undo"].append(snap)
    del stacks["undo"][:-STACK_LIMIT]
    # A fresh edit invalidates the redo branch, exactly like a text editor.
    stacks["redo"] = []
    request.session.modified = True


def state(request, order: SaleOrder) -> dict:
    """{can_undo, can_redo} for rendering the toolbar."""
    store = request.session.get(SESSION_KEY, {})
    stacks = store.get(str(order.pk), {})
    return {
        "can_undo": bool(stacks.get("undo")),
        "can_redo": bool(stacks.get("redo")),
    }


@transaction.atomic
def _restore(order: SaleOrder, snap: dict) -> None:
    """Rewrite the draft to match `snap`. Quantities are re-clamped against
    CURRENT availability: the snapshot was valid when it was taken, but stock
    can have moved since (another seller, a new production reservation), and an
    undo must never be the thing that puts an unsellable quantity in the cart.
    confirm_sale re-checks regardless — this just keeps the cart honest."""
    from apps.inventory.models import ProductVariant
    from apps.inventory.services import available_by_variant

    order.items.all().delete()
    variant_ids = [row["variant_id"] for row in snap["items"]]
    variants = {v.pk: v for v in ProductVariant.objects.filter(pk__in=variant_ids)}
    available = available_by_variant(variant_ids=variant_ids)

    used: dict[int, int] = {}
    for row in snap["items"]:
        variant = variants.get(row["variant_id"])
        if variant is None:  # variant deleted since the snapshot — drop the line
            continue
        room = available.get(variant.pk, 0) - used.get(variant.pk, 0)
        qty = min(row["quantity"], max(room, 0))
        if qty <= 0:
            continue
        used[variant.pk] = used.get(variant.pk, 0) + qty
        item = SaleItem(
            order=order,
            variant=variant,
            quantity=qty,
            unit_price=Decimal(row["unit_price"]),
            discount_type=row["discount_type"],
            discount_value=Decimal(row["discount_value"]),
        )
        # A fixed discount snapshotted against a larger quantity would break
        # saleitem_discount_fixed_lte_subtotal once the qty is clamped down.
        if item.discount_type == SaleItem.DISCOUNT_FIXED:
            item.discount_value = min(item.discount_value, item.subtotal)
        item.save()

    order.client_id = snap["client_id"]
    order.save(update_fields=["client"])


def undo(request, order: SaleOrder) -> bool:
    stacks = _stacks(request.session, order.pk)
    if not stacks["undo"]:
        return False
    stacks["redo"].append(_snapshot(order))
    del stacks["redo"][:-STACK_LIMIT]
    _restore(order, stacks["undo"].pop())
    request.session.modified = True
    return True


def redo(request, order: SaleOrder) -> bool:
    stacks = _stacks(request.session, order.pk)
    if not stacks["redo"]:
        return False
    stacks["undo"].append(_snapshot(order))
    del stacks["undo"][:-STACK_LIMIT]
    _restore(order, stacks["redo"].pop())
    request.session.modified = True
    return True


def clear(request, order_pk: int) -> None:
    """Drop a draft's history once it's confirmed — the sale is committed and
    its undo path is «Отменить продажа»/«Оформить возврат», not this."""
    store = request.session.get(SESSION_KEY, {})
    if store.pop(str(order_pk), None) is not None:
        request.session.modified = True
