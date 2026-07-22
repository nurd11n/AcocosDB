"""Work notes & tasks — the team's shared scratchpad to gather everything about
the work in one place. Any note doubles as a task via `done`; `pinned` floats it
to the top. Deliberately simple: plain text, no markdown engine (the strict CSP
forbids the client-side renderers), line breaks preserved on display."""

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class Note(models.Model):
    title = models.CharField(_("title"), max_length=200, blank=True)
    body = models.TextField(_("body"), blank=True)
    done = models.BooleanField(_("done"), default=False)
    # Set the moment `done` flips True, cleared the moment it flips back False —
    # only ever written by services.set_note_done(), never by hand. Drives
    # purge_completed_notes: a note stays exactly 4 weeks after its LATEST
    # completion, and un-completing resets that clock (completed_at=None takes
    # it out of the purge query entirely, regardless of how old it once was).
    completed_at = models.DateTimeField(_("completed at"), null=True, blank=True)
    pinned = models.BooleanField(_("pinned"), default=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("created by"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    class Meta:
        verbose_name = _("note")
        verbose_name_plural = _("notes")
        # active pinned first, then active, then done at the bottom; recent first.
        ordering = ["done", "-pinned", "-updated_at"]

    def __str__(self):
        return self.title or (self.body[:40] if self.body else _("(empty note)"))
