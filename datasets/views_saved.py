"""Named, shareable saved views.

Explorer state lived in one browser's localStorage. This gives it a name, an owner, and the
same three-level visibility as everything else (``core.access``): mine, my laboratory's,
everyone's.

Two things this file is careful about.

**Sharing a view must never widen access to data.** A saved view is a *configuration* — a
chart type, a group-by, a set of filters — and a sharing feature is exactly where an
access-control bypass likes to hide. If somebody publishes an organisation-wide view of a
Crude-Distillation dataset, an analyst in Lubricants must not get the rows by opening it, and
must not even see that the view exists: a view titled *"Crude Distillation — off-spec
batches"* leaks the dataset by leaking its name. So the queryset requires the user to be able
to see **both the view and the dataset behind it** (``SavedView.visible_to``), and there is a
test that says so.

**Changing who can see a view is a security-relevant act**, so it goes through
``core.audit.record_change`` like any other controlled record: the trail records that the
visibility went from ``private`` to ``org``, and who did it.
"""
from __future__ import annotations

from rest_framework import serializers, viewsets
from rest_framework.permissions import IsAuthenticated

from core.access import can_edit
from core.audit import record_audit, record_change, snapshot
from core.models import AuditLog

from .models import SavedView


class SavedViewSerializer(serializers.ModelSerializer):
    owner_username = serializers.CharField(source="owner.username", read_only=True)
    dataset_name = serializers.CharField(source="dataset.name", read_only=True)
    lab_name = serializers.CharField(source="lab.name", read_only=True, default=None)
    is_mine = serializers.SerializerMethodField()

    class Meta:
        model = SavedView
        fields = [
            "id", "name", "description", "dataset", "dataset_name", "page", "config",
            "owner", "owner_username", "is_mine", "lab", "lab_name",
            "visibility", "shared_with", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "owner", "created_at", "updated_at"]

    def get_is_mine(self, obj):
        user = self.context.get("request").user if self.context.get("request") else None
        return bool(user and obj.owner_id == user.id)

    def validate_dataset(self, dataset):
        """You cannot save a view of a dataset you cannot see.

        Without this, the create endpoint is a way to confirm that a dataset id exists and to
        attach a name of your choosing to it — and then, if the visibility rules were applied
        to the view rather than to the data, to hand it to somebody else.
        """
        user = self.context["request"].user
        if not dataset.accessible_by(user):
            raise serializers.ValidationError("That is not a dataset you can see.")
        return dataset


class SavedViewViewSet(viewsets.ModelViewSet):
    serializer_class = SavedViewSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return SavedView.visible_to(self.request.user)

    def perform_create(self, serializer):
        view = serializer.save(owner=self.request.user)
        record_audit(self.request, AuditLog.Action.CREATE, target_type="SavedView",
                     target_id=view.id,
                     summary=f"Saved view '{view.name}' ({view.get_visibility_display()})")

    def perform_update(self, serializer):
        # Only the owner (or an admin) may change a view. Being able to *open* somebody's
        # shared view is not a licence to rewrite it under them — the whole point of naming
        # and sharing it is that it stays the thing they shared.
        if not can_edit(serializer.instance, self.request.user):
            from rest_framework.exceptions import PermissionDenied

            raise PermissionDenied("That is not your view.")

        before = snapshot(serializer.instance)
        view = serializer.save()
        # record_change gives the before/after — so widening a share from `private` to `org`
        # shows up in the trail as exactly that, with a name against it.
        record_change(self.request, AuditLog.Action.UPDATE, view, before=before,
                      target_type="SavedView", summary=f"Updated view '{view.name}'")

    def perform_destroy(self, instance):
        if not can_edit(instance, self.request.user):
            from rest_framework.exceptions import PermissionDenied

            raise PermissionDenied("That is not your view.")
        record_change(self.request, AuditLog.Action.DELETE, instance,
                      target_type="SavedView", summary=f"Deleted view '{instance.name}'")
        instance.delete()
