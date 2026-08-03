"""Public (no login) surfaces reachable outside /pos/'s auth gate — currently
just the signed receipt link + its PDF. Kept OUT of views.py, where every
view sits behind @pos_view, so the trust boundary is obvious at a glance:
nothing in this file may assume request.user is authenticated. The signed,
expiring token (apps.pos.receipts) IS the access control — there is no
login, no permission check, and no sequential ID to guess here on purpose.
"""

from pathlib import Path

from django.conf import settings
from django.http import Http404, HttpResponse
from django.shortcuts import render
from django.template.loader import render_to_string

from .receipts import receipt_context, resolve_receipt_token


def receipt_public(request, token):
    order = resolve_receipt_token(token)
    if order is None:
        raise Http404
    return render(request, "receipts/receipt.html", receipt_context(order, token=token))


def receipt_pdf(request, token):
    order = resolve_receipt_token(token)
    if order is None:
        raise Http404
    # weasyprint imported lazily — a native-lib import failure (missing
    # cairo/pango, see docker/Dockerfile) should only break the PDF button,
    # never the rest of the app.
    from weasyprint import CSS, HTML

    html = render_to_string(
        "receipts/receipt.html", receipt_context(order, token=token, for_pdf=True)
    )
    # Passed directly rather than via the page's <link> (which for_pdf=True
    # omits) — WeasyPrint would otherwise try to fetch that URL over HTTP
    # through base_url, calling back into this same server mid-request.
    css_path = Path(settings.BASE_DIR) / "static" / "pos" / "receipt.css"
    pdf_bytes = HTML(string=html).write_pdf(stylesheets=[CSS(filename=str(css_path))])

    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="receipt-{order.pk}.pdf"'
    return response
