"""A shared two-step confirmation gate for admin bulk actions that mutate
money/stock/messaging and were previously firing the instant "Go" was
clicked — no confirmation step at all, unlike SaleOrderAdmin.approve_selected
(the one action that already asks first). Generalizes that existing pattern
instead of duplicating it per action.
"""

from django.template.response import TemplateResponse


def confirm_bulk_action(request, queryset, *, action_name, title, warning, admin_site, model_opts):
    """None once the user has confirmed (request.POST has 'apply') — the
    caller then proceeds with the real action. Otherwise a TemplateResponse
    for the intermediate confirm page, listing exactly what's selected."""
    if request.POST.get("apply"):
        return None
    return TemplateResponse(
        request,
        "admin/confirm_bulk_action.html",
        {
            **admin_site.each_context(request),
            "title": title,
            "warning": warning,
            "items": list(queryset),
            "selected": [str(pk) for pk in queryset.values_list("pk", flat=True)],
            "action_name": action_name,
            "opts": model_opts,
        },
    )
