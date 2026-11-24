"""Writing widgets through the API: the board's owner may, a viewer of the board may not.

Regression: ``WidgetViewSet._ensure_access`` called ``can_edit`` without importing it,
so every widget create, edit, reorder or move answered 500 for every user."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from dashboards.models import Dashboard, Widget

User = get_user_model()


class WidgetWriteAccessTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("board-owner", "o@x.co", "x", role="analyst")
        self.other = User.objects.create_user("board-reader", "r@x.co", "x", role="analyst")
        self.board = Dashboard.objects.create(name="Board", owner=self.owner, visibility="org")
        self.widget = Widget.objects.create(dashboard=self.board, title="KPI", widget_type="text",
                                            x=0, y=0, w=4, h=4, order=0)

    def client_for(self, user):
        client = APIClient()
        client.force_authenticate(user)
        return client

    def test_owner_creates_and_moves_widgets(self):
        api = self.client_for(self.owner)
        created = api.post("/api/widgets/", {"dashboard": self.board.id, "title": "Note",
                                             "widget_type": "text", "x": 0, "y": 4, "w": 6, "h": 2},
                           format="json")
        self.assertEqual(created.status_code, 201, created.content[:300])
        moved = api.patch(f"/api/widgets/{self.widget.id}/", {"x": 6, "y": 4, "w": 6, "h": 2},
                          format="json")
        self.assertEqual(moved.status_code, 200, moved.content[:300])
        self.widget.refresh_from_db()
        self.assertEqual((self.widget.x, self.widget.y, self.widget.w, self.widget.h), (6, 4, 6, 2))

    def test_seeing_a_board_is_not_editing_it(self):
        response = self.client_for(self.other).patch(f"/api/widgets/{self.widget.id}/",
                                                     {"order": 3}, format="json")
        self.assertIn(response.status_code, (403, 404))
        self.widget.refresh_from_db()
        self.assertEqual(self.widget.order, 0)

    def test_the_board_says_who_may_edit_it(self):
        def can_edit(user):
            response = self.client_for(user).get(f"/api/dashboards/{self.board.id}/")
            self.assertEqual(response.status_code, 200)
            return response.json()["can_edit"]
        self.assertTrue(can_edit(self.owner))
        self.assertFalse(can_edit(self.other))
        self.board.shared_with.add(self.other)
        self.assertTrue(can_edit(self.other))

    def test_a_user_the_board_is_shared_with_may_edit(self):
        self.board.shared_with.add(self.other)
        response = self.client_for(self.other).patch(f"/api/widgets/{self.widget.id}/",
                                                     {"order": 3}, format="json")
        self.assertEqual(response.status_code, 200, response.content[:300])
