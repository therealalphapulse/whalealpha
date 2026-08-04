"""Unit tests for engines/wallet_graph.py's pure relationship-strength
functions — no DB, no network."""

from __future__ import annotations

from whale_alpha.engines.wallet_graph import (
    RelationshipState,
    compute_strength,
    update_relationship,
)


def test_first_observation_creates_a_relationship_with_count_one():
    state = update_relationship(None, "MINT_A")
    assert state.co_occurrence_count == 1
    assert state.shared_token_mints == ("MINT_A",)


def test_a_new_shared_mint_increments_the_count():
    state = update_relationship(None, "MINT_A")
    state = update_relationship(state, "MINT_B")
    assert state.co_occurrence_count == 2
    assert state.shared_token_mints == ("MINT_A", "MINT_B")


def test_re_observing_the_same_mint_does_not_inflate_the_count():
    state = update_relationship(None, "MINT_A")
    state = update_relationship(state, "MINT_A")
    assert state.co_occurrence_count == 1


def test_strength_increases_monotonically_with_cooccurrence_but_never_reaches_one():
    strengths = [compute_strength(n) for n in range(1, 10)]
    assert strengths == sorted(strengths)
    assert all(0 <= s < 1 for s in strengths)


def test_strength_field_on_state_matches_compute_strength():
    state = RelationshipState(co_occurrence_count=3, shared_token_mints=("A", "B", "C"), strength=0.0)
    updated = update_relationship(state, "D")
    assert updated.strength == compute_strength(4)
