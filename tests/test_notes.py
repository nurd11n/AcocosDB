"""Note completion lifecycle (services.py) and the 4-week purge job.

Timezone forced to Asia/Bishkek to match the project's other date-boundary
tests (tests/test_flows.py) — irrelevant to the purge math itself (it's a
timedelta off timezone.now(), not a local-date boundary), but keeping the same
discipline means these tests stay correct regardless of the ambient TIME_ZONE.
"""

import datetime

import pytest
from django.core.management import call_command
from django.utils import timezone

from apps.notes.models import Note
from apps.notes.services import set_note_done

pytestmark = pytest.mark.django_db


def _freeze_now(monkeypatch, y, mo, d, h=12, mi=0):
    fixed = datetime.datetime(y, mo, d, h, mi, tzinfo=datetime.timezone.utc)
    monkeypatch.setattr("django.utils.timezone.now", lambda: fixed)


def _completed_days_ago(days, y=2026, mo=7, d=21):
    """A note whose completed_at is exactly `days` before the fixed "now" used
    by these tests (2026-07-21 12:00 UTC)."""
    now = datetime.datetime(y, mo, d, 12, 0, tzinfo=datetime.timezone.utc)
    completed = now - datetime.timedelta(days=days)
    note = Note.objects.create(title=f"done {days}d ago", done=True)
    Note.objects.filter(pk=note.pk).update(completed_at=completed)
    return note


def test_marking_done_sets_completed_at(monkeypatch):
    with timezone.override("Asia/Bishkek"):
        _freeze_now(monkeypatch, 2026, 7, 21)
        note = Note.objects.create(title="a task")
        assert note.completed_at is None

        set_note_done(note, True)
        note.refresh_from_db()
        assert note.done is True
        assert note.completed_at == datetime.datetime(
            2026, 7, 21, 12, 0, tzinfo=datetime.timezone.utc
        )


def test_uncompleting_clears_completed_at_and_resets_the_clock(monkeypatch):
    with timezone.override("Asia/Bishkek"):
        note = _completed_days_ago(40)  # long overdue for purge...
        set_note_done(note, False)  # ...but un-completed
        note.refresh_from_db()
        assert note.done is False
        assert note.completed_at is None


def test_purge_deletes_notes_completed_28_or_more_days_ago(monkeypatch):
    with timezone.override("Asia/Bishkek"):
        _freeze_now(monkeypatch, 2026, 7, 21)
        old = _completed_days_ago(28)  # exactly the boundary — must go
        older = _completed_days_ago(40)
        call_command("purge_completed_notes")
        assert not Note.objects.filter(pk=old.pk).exists()
        assert not Note.objects.filter(pk=older.pk).exists()


def test_purge_keeps_notes_completed_27_days_ago(monkeypatch):
    with timezone.override("Asia/Bishkek"):
        _freeze_now(monkeypatch, 2026, 7, 21)
        recent = _completed_days_ago(27)
        call_command("purge_completed_notes")
        assert Note.objects.filter(pk=recent.pk).exists()


def test_purge_keeps_uncompleted_note_even_if_done_40_days_ago_before_reset(monkeypatch):
    # Completed 40 days ago, then un-completed (clock reset to None) — must
    # survive the purge regardless of the stale original completion date.
    with timezone.override("Asia/Bishkek"):
        _freeze_now(monkeypatch, 2026, 7, 21)
        note = _completed_days_ago(40)
        set_note_done(note, False)

        call_command("purge_completed_notes")
        assert Note.objects.filter(pk=note.pk).exists()
        note.refresh_from_db()
        assert note.completed_at is None
        assert note.done is False


def test_purge_is_idempotent_and_leaves_active_notes_alone(monkeypatch):
    with timezone.override("Asia/Bishkek"):
        _freeze_now(monkeypatch, 2026, 7, 21)
        active = Note.objects.create(title="still working on it", done=False)
        stale = _completed_days_ago(30)

        call_command("purge_completed_notes")
        call_command("purge_completed_notes")  # re-run: must not error or double-count

        assert Note.objects.filter(pk=active.pk).exists()
        assert not Note.objects.filter(pk=stale.pk).exists()


# ---- Role boundaries on the shared scratchpad -----------------------------


@pytest.mark.django_db
def test_notes_role_matrix_owner_editor_viewer(client, django_user_model):
    """Regression: every notes view was gated on login_required ALONE, and no
    role held a notes permission — so a Viewer, documented read-only, could
    create, edit, toggle, pin and permanently DELETE another user's note.
    Owner does everything; Editor writes but cannot delete (setup_roles grants
    add/change/view only, like every other business model); Viewer reads."""
    from django.contrib.auth.models import Group
    from django.core.management import call_command

    from apps.core.permissions import EDITOR, VIEWER
    from apps.notes.models import Note

    call_command("setup_roles", verbosity=0)
    owner = django_user_model.objects.create_superuser("nm_owner", "o@e.com", "x" * 12)
    editor = django_user_model.objects.create_user("nm_editor", password="x" * 12, is_staff=True)
    editor.groups.add(Group.objects.get(name=EDITOR))
    viewer = django_user_model.objects.create_user("nm_viewer", password="x" * 12, is_staff=True)
    viewer.groups.add(Group.objects.get(name=VIEWER))

    expected = {
        "owner": dict(create=True, edit=True, delete=True),
        "editor": dict(create=True, edit=True, delete=False),
        "viewer": dict(create=False, edit=False, delete=False),
    }
    for label, user in (("owner", owner), ("editor", editor), ("viewer", viewer)):
        client.force_login(user)
        note = Note.objects.create(title="seed", created_by=owner)
        want = expected[label]

        before = Note.objects.count()
        client.post("/notes/", {"title": "made", "body": "b"})
        assert (Note.objects.count() > before) is want["create"], f"{label}: create"

        client.post(f"/notes/{note.pk}/edit/", {"title": "edited", "body": ""})
        note.refresh_from_db()
        assert (note.title == "edited") is want["edit"], f"{label}: edit"

        client.post(f"/notes/{note.pk}/delete/")
        assert (not Note.objects.filter(pk=note.pk).exists()) is want["delete"], f"{label}: delete"

        # Everyone can still READ the board — it's a shared scratchpad.
        assert client.get("/notes/").status_code == 200, f"{label}: cannot read"
