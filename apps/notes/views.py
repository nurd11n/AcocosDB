"""Work-notes board. Plain form POSTs + redirects (no inline JS, CSP-safe).
Every logged-in POS user shares one board — it's a team scratchpad."""

from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.pos.decorators import pos_view

from .models import Note
from .services import set_note_done


@pos_view
def index(request):
    if request.method == "POST":
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
        {"notes": notes, "active_count": active_count, "active": "notes"},
    )


@pos_view
@require_POST
def toggle(request, pk):
    note = get_object_or_404(Note, pk=pk)
    set_note_done(note, not note.done)
    return redirect("notes:index")


@pos_view
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
        note.title = request.POST.get("title", "").strip()[:200]
        note.body = request.POST.get("body", "").strip()
        note.save()
        return redirect("notes:index")
    return render(request, "notes/edit.html", {"note": note, "active": "notes"})


@pos_view
@require_POST
def delete(request, pk):
    get_object_or_404(Note, pk=pk).delete()
    return redirect("notes:index")
