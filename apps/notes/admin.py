from django.contrib import admin

from .models import Note
from .services import apply_done


@admin.register(Note)
class NoteAdmin(admin.ModelAdmin):
    list_display = ("__str__", "done", "completed_at", "pinned", "created_by", "updated_at")
    list_filter = ("done", "pinned")
    search_fields = ("title", "body")
    readonly_fields = ("completed_at",)  # derived from `done` via services.apply_done, never typed

    def save_model(self, request, obj, form, change):
        # Route the done/undone transition through the SAME rule the POS
        # toggle uses (services.apply_done), so completed_at is never set by
        # hand here either — then save normally so any other edited fields
        # (title, body, pinned) persist in the same write.
        if "done" in form.changed_data:
            apply_done(obj, obj.done)
        super().save_model(request, obj, form, change)
