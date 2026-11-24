from rest_framework import serializers

from .models import Dashboard, Widget


class WidgetSerializer(serializers.ModelSerializer):
    dataset_name = serializers.CharField(source="dataset.name", read_only=True)

    class Meta:
        model = Widget
        fields = [
            "id", "dashboard", "title", "widget_type", "dataset", "dataset_name",
            "config", "x", "y", "w", "h", "order", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class DashboardSerializer(serializers.ModelSerializer):
    widgets = WidgetSerializer(many=True, read_only=True)
    owner_username = serializers.CharField(source="owner.username", read_only=True)
    widget_count = serializers.IntegerField(source="widgets.count", read_only=True)
    lab_name = serializers.CharField(source="lab.name", read_only=True, default=None)
    datasource_names = serializers.SerializerMethodField()
    freshness = serializers.SerializerMethodField()
    can_edit = serializers.SerializerMethodField()

    class Meta:
        model = Dashboard
        fields = [
            "id", "name", "description", "owner", "owner_username", "site",
            "lab", "lab_name", "visibility", "shared_with", "filters", "widgets",
            "widget_count",
            "datasource_names", "freshness", "can_edit", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "owner", "created_at", "updated_at"]

    def get_can_edit(self, obj):
        """May the requesting user change this board's widgets? The same rule the widget
        API enforces (WidgetViewSet._ensure_access), so the page only offers editing
        controls that will work: seeing a board is not editing it."""
        from core.access import can_edit
        request = self.context.get("request")
        user = getattr(request, "user", None)
        if not (user and user.is_authenticated):
            return False
        return bool(can_edit(obj, user) or obj.shared_with.filter(pk=user.pk).exists())

    def get_datasource_names(self, obj):
        """Distinct source databases this dashboard's widgets read from."""
        names = set()
        for w in obj.widgets.all():
            ds = getattr(w, "dataset", None)
            if ds and ds.query_id and ds.query.datasource_id:
                names.add(ds.query.datasource.name)
        return sorted(names)

    def get_freshness(self, obj):
        """How current the board's numbers are, in one line for its header.

        The oldest data among the board's sources bounds the board (the same rule as an
        answer): a restored copy reports its backup time ("snapshot"), a replica its
        oldest sync, a live source "live". Computed per source and cached there.
        """
        from replication.freshness import describe

        sources = {}
        for w in obj.widgets.all():
            ds = getattr(w, "dataset", None)
            if ds and ds.query_id and ds.query.datasource_id:
                sources[ds.query.datasource_id] = ds.query.datasource
        if not sources:
            return None
        rank = {"stale": 0, "unknown": 1, "snapshot": 2, "replica": 3, "live": 4}
        found = [describe(source) for source in sources.values()]
        worst = min(found, key=lambda f: (rank.get(f.kind, 1), f.data_as_of or "9999"))
        return {"kind": worst.kind, "data_as_of": worst.data_as_of, "detail": worst.detail}

    def create(self, validated_data):
        shared = validated_data.pop("shared_with", [])
        validated_data["owner"] = self.context["request"].user
        dashboard = Dashboard.objects.create(**validated_data)
        dashboard.shared_with.set(shared)
        return dashboard

    def update(self, instance, validated_data):
        shared = validated_data.pop("shared_with", None)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        if shared is not None:
            instance.shared_with.set(shared)
        return instance
