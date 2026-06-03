"""Admin registration for the knowledge-base staging model."""
from django.contrib import admin

from . import ingest, index_store
from .models import KnowledgeRecord


@admin.register(KnowledgeRecord)
class KnowledgeRecordAdmin(admin.ModelAdmin):
    list_display = (
        "doc_id", "source_type", "status", "passage_count",
        "token_estimate", "created_by", "reviewed_by", "created_at",
    )
    list_filter = ("status", "source_type")
    search_fields = ("doc_id", "source_file", "content_hash")
    readonly_fields = (
        "content_hash", "passages", "validation", "passage_count",
        "token_estimate", "duplicate_of", "created_by", "reviewed_by",
        "reviewed_at", "created_at", "updated_at",
    )
    actions = ["approve_selected", "reject_selected"]

    @admin.action(description="Approve selected records and reindex")
    def approve_selected(self, request, queryset):
        approved = 0
        for rec in queryset:
            try:
                ingest.approve_record(rec, reviewed_by=request.user, reindex=False)
                approved += 1
            except ValueError as exc:
                self.message_user(request, f"{rec.doc_id}: {exc}", level="warning")
        if approved:
            index_store.rebuild_index()
        self.message_user(request, f"Approved {approved} record(s); index rebuilt.")

    @admin.action(description="Reject selected records")
    def reject_selected(self, request, queryset):
        for rec in queryset:
            ingest.reject_record(rec, reviewed_by=request.user, reason="Rejected via admin")
        self.message_user(request, f"Rejected {queryset.count()} record(s).")
