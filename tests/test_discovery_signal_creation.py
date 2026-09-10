"""
Regression coverage for the Discovery Engine A/B -> SignalToken wiring
fix (milestone alerts were never quoting for Wallet Consensus /
Robinhood Chain signals because neither engine ever created a
SignalToken row -- see domain.signals.signal_tracker.
create_signal_from_candidate()'s new enforce_pumpfun_policy/chain
params, and domain.signals.wallet_consensus_engine /
domain.signals.robinhood_discovery's is_first_alert wiring).

DB-backed behavior (the actual INSERT, migration, and lifecycle-loop
price re-poll) isn't exercised here -- there's no DB fixture in this
test suite to drive it against. This locks down the one thing that's
easy to silently regress without a DB: the function's signature and
default values staying backward compatible for the original caller
(domain.signals.pump_radar's classic Pump.fun-only loop), which never
passes these new kwargs.
"""

import inspect
import unittest

from domain.signals.signal_tracker import create_signal_from_candidate


class CreateSignalFromCandidateSignatureTests(unittest.TestCase):
    def test_new_params_are_keyword_only_with_backward_compatible_defaults(self):
        sig = inspect.signature(create_signal_from_candidate)
        params = sig.parameters

        self.assertIn("enforce_pumpfun_policy", params)
        self.assertIn("chain", params)

        # Backward compatible: the original caller (pump_radar.py) calls
        # create_signal_from_candidate(candidate) with no other args --
        # that must still enforce the Pump.fun-only policy and default
        # to "solana", exactly as before this change existed.
        self.assertEqual(params["enforce_pumpfun_policy"].default, True)
        self.assertEqual(params["chain"].default, "solana")

        # Keyword-only (appear after the bare `candidate` positional) --
        # a positional call from any existing caller can never
        # accidentally supply these and flip the policy off.
        self.assertEqual(params["enforce_pumpfun_policy"].kind, inspect.Parameter.KEYWORD_ONLY)
        self.assertEqual(params["chain"].kind, inspect.Parameter.KEYWORD_ONLY)

    def test_candidate_param_still_the_only_positional_argument(self):
        sig = inspect.signature(create_signal_from_candidate)
        positional = [
            p for p in sig.parameters.values()
            if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
        self.assertEqual([p.name for p in positional], ["candidate"])


if __name__ == "__main__":
    unittest.main()
