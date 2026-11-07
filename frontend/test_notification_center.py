"""Unit tests for Notification Center key generation and data robustness."""

import unittest


def generate_notification_keys(notifications: list[dict]) -> list[str]:
    """Mirror the key generation logic in frontend/app.py."""
    keys = []
    for idx, notif in enumerate(notifications):
        raw_id = notif.get("id") or notif.get("notification_id")
        n_id = str(raw_id).strip() if raw_id is not None and str(raw_id).strip() else None
        btn_key = f"btn_read_{n_id}_{idx}" if n_id else f"btn_read_pos_{idx}"
        keys.append(btn_key)
    return keys


class NotificationCenterKeyTests(unittest.TestCase):
    def test_empty_notifications(self):
        keys = generate_notification_keys([])
        self.assertEqual(keys, [])

    def test_single_notification_valid_id(self):
        notifs = [{"id": "notif-001", "is_read": 0, "title": "Test"}]
        keys = generate_notification_keys(notifs)
        self.assertEqual(keys, ["btn_read_notif-001_0"])
        self.assertEqual(len(keys), len(set(keys)))

    def test_multiple_notifications_unique_ids(self):
        notifs = [
            {"id": "notif-001", "is_read": 0},
            {"id": "notif-002", "is_read": 0},
            {"notification_id": "notif-003", "is_read": 0},
        ]
        keys = generate_notification_keys(notifs)
        self.assertEqual(len(keys), 3)
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(keys[0], "btn_read_notif-001_0")
        self.assertEqual(keys[1], "btn_read_notif-002_1")
        self.assertEqual(keys[2], "btn_read_notif-003_2")

    def test_missing_ids_produces_unique_stable_keys(self):
        notifs = [
            {"is_read": 0, "title": "Alert 1"},
            {"id": None, "is_read": 0, "title": "Alert 2"},
            {"notification_id": "", "is_read": 0, "title": "Alert 3"},
        ]
        keys = generate_notification_keys(notifs)
        self.assertEqual(len(keys), 3)
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(keys, ["btn_read_pos_0", "btn_read_pos_1", "btn_read_pos_2"])

    def test_duplicate_ids_produce_unique_stable_keys(self):
        notifs = [
            {"id": "duplicate-id", "is_read": 0},
            {"id": "duplicate-id", "is_read": 0},
            {"notification_id": "duplicate-id", "is_read": 0},
        ]
        keys = generate_notification_keys(notifs)
        self.assertEqual(len(keys), 3)
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(keys, [
            "btn_read_duplicate-id_0",
            "btn_read_duplicate-id_1",
            "btn_read_duplicate-id_2",
        ])

    def test_mixed_malformed_and_valid_ids(self):
        notifs = [
            {"id": "valid-1", "is_read": 0},
            {"id": None, "is_read": 0},
            {"id": "   ", "is_read": 0},
            {"notification_id": "valid-1", "is_read": 0},
            {"id": 12345, "is_read": 0},
        ]
        keys = generate_notification_keys(notifs)
        self.assertEqual(len(keys), 5)
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(keys[0], "btn_read_valid-1_0")
        self.assertEqual(keys[1], "btn_read_pos_1")
        self.assertEqual(keys[2], "btn_read_pos_2")
        self.assertEqual(keys[3], "btn_read_valid-1_3")
        self.assertEqual(keys[4], "btn_read_12345_4")

    def test_keys_are_deterministic_across_runs(self):
        notifs = [
            {"id": "notif-abc", "is_read": 0},
            {"id": None, "is_read": 0},
        ]
        run1 = generate_notification_keys(notifs)
        run2 = generate_notification_keys(notifs)
        self.assertEqual(run1, run2)


if __name__ == "__main__":
    unittest.main()

