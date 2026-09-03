"""
tests/test_pump_alert_channel_config.py

Regression coverage for the PUMP_ALERT_CHANNEL_IDS channel-routing fix.

Root cause under test: Signal Alerts (domain/signals/pump_radar.py::
get_pump_subscribers -> _load_channel_ids) already accepted both numeric
chat IDs and public "@username" channel handles (e.g. "@therealalphapulse")
from the shared PUMP_ALERT_CHANNEL_IDS env var. Quote Milestone alerts
(domain/signals/signal_tracker.py::send_milestone_alert) parsed the exact
same env var a second time with its own inline int-only loop, silently
dropping any "@username" entry via `except ValueError: pass`. The result:
a channel configured for alerts would receive Signal Alerts but never
Quote Milestone alerts.

This file verifies both the shared parser's behavior and that
send_milestone_alert no longer reimplements its own duplicate parser.
"""

import inspect
import os
import unittest

os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost/test")

from domain.signals.pump_radar import _load_channel_ids  # noqa: E402
from domain.signals import signal_tracker  # noqa: E402


class LoadChannelIdsTests(unittest.TestCase):
    """Shared parser used by both Signal Alerts and Quote Milestone alerts."""

    def setUp(self):
        self._orig = os.environ.get("PUMP_ALERT_CHANNEL_IDS")

    def tearDown(self):
        if self._orig is None:
            os.environ.pop("PUMP_ALERT_CHANNEL_IDS", None)
        else:
            os.environ["PUMP_ALERT_CHANNEL_IDS"] = self._orig

    def test_empty_env_returns_empty_list(self):
        os.environ["PUMP_ALERT_CHANNEL_IDS"] = ""
        self.assertEqual(_load_channel_ids(), [])

    def test_numeric_chat_id_parsed_as_int(self):
        os.environ["PUMP_ALERT_CHANNEL_IDS"] = "-1001234567890"
        self.assertEqual(_load_channel_ids(), [-1001234567890])

    def test_public_channel_username_kept_as_string(self):
        os.environ["PUMP_ALERT_CHANNEL_IDS"] = "@therealalphapulse"
        self.assertEqual(_load_channel_ids(), ["@therealalphapulse"])

    def test_mixed_numeric_and_username_entries(self):
        os.environ["PUMP_ALERT_CHANNEL_IDS"] = "-1001234567890, @therealalphapulse"
        self.assertEqual(
            _load_channel_ids(), [-1001234567890, "@therealalphapulse"]
        )


class SendMilestoneAlertUsesSharedParserTests(unittest.TestCase):
    """Guards against reintroducing a second, duplicated env-var parser."""

    def test_send_milestone_alert_imports_shared_channel_loader(self):
        src = inspect.getsource(signal_tracker.send_milestone_alert)
        self.assertIn("_load_channel_ids", src)

    def test_send_milestone_alert_no_longer_hand_rolls_int_only_parsing(self):
        src = inspect.getsource(signal_tracker.send_milestone_alert)
        self.assertNotIn("os.getenv(\"PUMP_ALERT_CHANNEL_IDS\"", src)


if __name__ == "__main__":
    unittest.main()
