from rest_framework import serializers

from .models import ChatMessage, Conversation


class ChatMessageSerializer(serializers.ModelSerializer):
    class Meta:
        model = ChatMessage
        fields = ["id", "role", "content", "metadata", "created_at"]


class ConversationListSerializer(serializers.ModelSerializer):
    message_count = serializers.IntegerField(source="messages.count", read_only=True)
    dataset_name = serializers.CharField(source="dataset.name", read_only=True)

    class Meta:
        model = Conversation
        fields = [
            "id", "title", "dataset", "dataset_name",
            "message_count", "created_at", "updated_at",
        ]


class ConversationSerializer(serializers.ModelSerializer):
    messages = ChatMessageSerializer(many=True, read_only=True)
    dataset_name = serializers.CharField(source="dataset.name", read_only=True)

    class Meta:
        model = Conversation
        fields = [
            "id", "title", "dataset", "dataset_name", "messages",
            "created_at", "updated_at",
        ]
