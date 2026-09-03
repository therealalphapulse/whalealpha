"""
Regression test for the SignalMilestoneGate delivery-confirmation bug.

Production symptom (Railway logs):
    "[SignalMilestoneGate] Suppressing milestone alert for 7MYegHoq
     (gain=11.42x) — initial Signal Alert not yet confirmed delivered"

Root cause: signal_lifecycle_loop() in domain/signals/signal_tracker.py
hard-gated milestone (Quote Alert) generation on
`has_confirmed_alert_delivery`, so a token that reached a qualifying
milestone gain never got an alert if its original Signal Alert send had
not (yet, or ever) been confirmed delivered.

Required behavior: a qualifying milestone must always be allowed to
generate, regardless of initial Signal Alert delivery confirmation.

This test executes the *actual* gate block bytecode straight out of
signal_tracker.py (via ast, isolating just that statement range) rather
than re-implementing the logic, so it fails if the real gate regresses,
without needing to boot the full async DB/network lifecycle loop.
"""
import ast
import math
import types
import unittest
from types import SimpleNamespace

SOURCE_PATH = "signal_tracker.py"


def _load_gate_block():
    """Extract the SignalMilestoneGate statements from
    signal_lifecycle_loop and compile them standalone, together with
    _milestones_crossed (their only real dependency)."""
    with open(SOURCE_PATH, "r", encoding="utf-8") as f:
        source = f.read()

    tree = ast.parse(source)

    milestones_crossed_fn = None
    gate_stmts = None

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_milestones_crossed":
            milestones_crossed_fn = node
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "signal_lifecycle_loop":
            gate_stmts = []
            collecting = False
            for stmt in ast.walk(node):
                pass  # placeholder, real extraction below

    assert milestones_crossed_fn is not None, "_milestones_crossed not found"

    # Extract the gate block as raw source lines (line-range slice), since
    # it's a straight-line sequence of statements inside the loop body, not
    # its own function -- this is the exact code Railway executes.
    lines = source.splitlines()
    start_marker = "has_confirmed_alert_delivery = bool("
    end_marker = "crossed_milestones = ("
    start_idx = next(i for i, l in enumerate(lines) if start_marker in l)
    cm_idx = next(i for i in range(start_idx, len(lines)) if end_marker in lines[i])
    # crossed_milestones = ( ... ) spans a few lines; find its closing paren.
    end_idx = next(
        i for i in range(cm_idx, len(lines))
        if lines[i].strip() == ")" and i > cm_idx
    )
    assert end_idx is not None, "could not locate end of gate block"

    block_lines = lines[start_idx : end_idx + 1]
    # De-indent (block is nested inside try/for/async-with/async-with -> 20 spaces)
    indent = len(block_lines[0]) - len(block_lines[0].lstrip(" "))
    block_src = "\n".join(l[indent:] if l.strip() else "" for l in block_lines)

    milestones_src = ast.get_source_segment(source, milestones_crossed_fn)

    return block_src, milestones_src


GATE_SRC, MILESTONES_SRC = _load_gate_block()


class _FakeLogger:
    def __init__(self):
        self.info_calls = []

    def info(self, msg, *args):
        self.info_calls.append(msg % args if args else msg)


def run_gate(*, message_ids_json, gain, is_currently_trading, last_alerted, contract="7MYegHoq_fullcontract"):
    """Execute the real, unmodified gate statements extracted from
    signal_tracker.py against a synthetic SignalToken-like row."""
    namespace = {"math": math, "_MAX_MILESTONES_PER_POLL": 500}
    exec(MILESTONES_SRC, namespace)  # defines _milestones_crossed

    s = SimpleNamespace(message_ids_json=message_ids_json, contract=contract)
    logger = _FakeLogger()

    namespace.update(
        {
            "s": s,
            "gain": gain,
            "is_currently_trading": is_currently_trading,
            "last_alerted": last_alerted,
            "logger": logger,
            "_milestones_crossed": namespace["_milestones_crossed"],
        }
    )

    exec(GATE_SRC, namespace)

    return namespace["crossed_milestones"], logger, namespace["has_confirmed_alert_delivery"]


class MilestoneGateAllowsUndeliveredInitialAlertTests(unittest.TestCase):
    """The exact production scenario: initial_signal_delivered=False
    (message_ids_json empty/None) and a qualifying milestone gain."""

    def test_qualifying_milestone_fires_when_initial_alert_undelivered(self):
        # Matches the production log line exactly: gain=11.42x, undelivered.
        crossed, logger, delivered = run_gate(
            message_ids_json=None,
            gain=11.42,
            is_currently_trading=True,
            last_alerted=1.0,
        )
        self.assertFalse(delivered)
        self.assertTrue(crossed, "milestone alert must be generated even when "
                                  "initial Signal Alert delivery is unconfirmed")
        # It should include every rung up through 11X.
        self.assertIn((11.0, "11X"), crossed)
        self.assertNotIn("Suppressing", " ".join(logger.info_calls))

    def test_undelivered_and_falsy_message_ids_json_also_fires(self):
        for falsy_value in (None, "", "{}"):
            with self.subTest(message_ids_json=falsy_value):
                crossed, _logger, delivered = run_gate(
                    message_ids_json=falsy_value,
                    gain=2.5,
                    is_currently_trading=True,
                    last_alerted=1.0,
                )
                self.assertFalse(delivered)
                self.assertTrue(crossed)

    def test_delivered_confirmed_still_fires_as_before(self):
        # Preserve existing behavior for the confirmed-delivery path.
        crossed, _logger, delivered = run_gate(
            message_ids_json='{"123": ["456"]}',
            gain=3.0,
            is_currently_trading=True,
            last_alerted=1.0,
        )
        self.assertTrue(delivered)
        self.assertEqual(crossed, [(1.25, "+25%"), (1.50, "+50%"), (2.0, "2X"), (3.0, "3X")])

    def test_not_currently_trading_still_suppresses_regardless_of_delivery(self):
        # Unrelated filter (24h trading volume gate) must be preserved.
        crossed, _logger, _delivered = run_gate(
            message_ids_json=None,
            gain=5.0,
            is_currently_trading=False,
            last_alerted=1.0,
        )
        self.assertEqual(crossed, [])

    def test_below_threshold_undelivered_produces_no_alert_and_no_log_spam(self):
        # Gain below the first rung: nothing to fire, and no misleading
        # "proceeding" log should be emitted either.
        crossed, logger, delivered = run_gate(
            message_ids_json=None,
            gain=1.10,
            is_currently_trading=True,
            last_alerted=1.0,
        )
        self.assertFalse(delivered)
        self.assertEqual(crossed, [])
        self.assertEqual(logger.info_calls, [])


if __name__ == "__main__":
    unittest.main()
