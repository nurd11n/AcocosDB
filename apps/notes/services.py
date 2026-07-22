"""Notes business logic — the one path both the POS UI and /panel/ admin go
through for state transitions, per CLAUDE.md (never reimplemented in a view).
"""

from django.utils import timezone

from .models import Note


def apply_done(note: Note, done: bool) -> Note:
    """The one rule: marking done stamps completed_at = now (starts the 4-week
    purge clock); un-marking clears it back to None (resets the clock — a note
    re-completed later starts counting from THAT moment, never a stale prior
    completion). Sets fields on the in-memory instance only — does not save,
    so a caller that's already about to persist other changed fields (the
    admin form) doesn't need a second write."""
    note.done = done
    note.completed_at = timezone.now() if done else None
    return note


def set_note_done(note: Note, done: bool) -> Note:
    """Flip + persist a note's completion state. Used by the POS toggle view,
    which only ever changes these two fields."""
    apply_done(note, done)
    note.save(update_fields=["done", "completed_at"])
    return note
