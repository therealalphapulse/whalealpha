"""Tests for the pure direction-matching helper in engines/price_alerts.py."""

from __future__ import annotations

from whale_alpha.engines.price_alerts import _direction_matches


def test_up_direction_matches_positive_change_only():
    assert _direction_matches("UP", 5.0) is True
    assert _direction_matches("UP", -5.0) is False


def test_down_direction_matches_negative_change_only():
    assert _direction_matches("DOWN", -5.0) is True
    assert _direction_matches("DOWN", 5.0) is False


def test_both_direction_matches_either_sign():
    assert _direction_matches("BOTH", 5.0) is True
    assert _direction_matches("BOTH", -5.0) is True
