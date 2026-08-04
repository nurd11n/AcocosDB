"""Work-notes board. Plain form POSTs + redirects (no inline JS, CSP-safe).
Every logged-in POS user shares one board — it's a team scratchpad.

"Shared" means shared between people who can WRITE. Reading is open to any
logged-in user; creating, editing, toggling, pinning and deleting are gated
on real notes permissions (see apps.core.permissions.BUSINESS_MODEL_PERMISSIONS — Editor
gets add/change/view, Viewer view-only, delete stays Owner-only like every
other business model). Before those gates existed NO role held a notes
permission and no view checked one, so a documented read-only Viewer could
create, edit and permanently DELETE another user's note.
"""

from functools import wraps

from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.pos.decorators import pos_view

from .models import Note
from .services import set_note_done


def _require(perm):
    """Gate a note write on a real Django permission, server-side — a Viewer
    POSTing the URL directly gets 403, not just a hidden button."""

    def decorator(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            if not request.user.has_perm(perm):
                raise PermissionDenied
            return view(request, *args, **kwargs)

        return wrapper

    return decorator


@pos_view
def index(request):
    if request.method == "POST":
        if not request.user.has_perm("notes.add_note"):
            raise PermissionDenied
        title = request.POST.get("title", "").strip()[:200]
        body = request.POST.get("body", "").strip()
        if title or body:
            Note.objects.create(title=title, body=body, created_by=request.user)
        return redirect("notes:index")

    notes = Note.objects.select_related("created_by").all()
    active_count = sum(1 for n in notes if not n.done)
    return render(
        request,
        "notes/index.html",
        {
            "notes": notes,
            "active_count": active_count,
            "can_write": request.user.has_perm("notes.add_note"),
            "can_edit": request.user.has_perm("notes.change_note"),
            "can_delete": request.user.has_perm("notes.delete_note"),
            "active": "notes",
        },
    )


@pos_view
@_require("notes.change_note")
@require_POST
def toggle(request, pk):
    note = get_object_or_404(Note, pk=pk)
    set_note_done(note, not note.done)
    return redirect("notes:index")


@pos_view
@_require("notes.change_note")
@require_POST
def pin(request, pk):
    note = get_object_or_404(Note, pk=pk)
    note.pinned = not note.pinned
    note.save()
    return redirect("notes:index")


@pos_view
def edit(request, pk):
    note = get_object_or_404(Note, pk=pk)
    if request.method == "POST":
        if not request.user.has_perm("notes.change_note"):
            raise PermissionDenied
        note.title = request.POST.get("title", "").strip()[:200]
        note.body = request.POST.get("body", "").strip()
        note.save()
        return redirect("notes:index")
    return render(
        request,
        "notes/edit.html",
        {
            "note": note,
            "can_edit": request.user.has_perm("notes.change_note"),
            "can_delete": request.user.has_perm("notes.delete_note"),
            "active": "notes",
        },
    )


@pos_view
@_require("notes.delete_note")
@require_POST
def delete(request, pk):
    get_object_or_404(Note, pk=pk).delete()
    return redirect("notes:index")
