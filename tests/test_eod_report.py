"""Tests for the End-of-Day Alert Report (domain/signals/signal_tracker.py's
build_daily_eod_report), scoped to: Powered by WhaleAlpha footer, Total %
Gained, and Win Rate -- the existing Total Alerts Sent / Performing /
Non-Performing counts and overall layout are unchanged and not retested
here beyond confirming they still appear."""

from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from domain.signals.signal_tracker import build_daily_eod_report


def _fake_signal(ath_multiple):
    return SimpleNamespace(ath_multiple=ath_multiple)


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        m = MagicMock()
        m.all.return_value = self._rows
        return m


class _FakeSession:
    def __init__(self, rows):
        self._rows = rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def execute(self, *args, **kwargs):
        return _FakeResult(self._rows)


@pytest.mark.asyncio
async def test_eod_report_footer_says_whalealpha_not_alphapulse():
    rows = [_fake_signal(1.5), _fake_signal(0.8)]
    with patch("domain.signals.signal_tracker.async_session", return_value=_FakeSession(rows)):
        report = await build_daily_eod_report(target_date=date(2026, 9, 27))

    assert "Powered by WhaleAlpha" in report
    assert "Powered by AlphaPulse" not in report


@pytest.mark.asyncio
async def test_eod_report_no_alerts_footer_says_whalealpha():
    with patch("domain.signals.signal_tracker.async_session", return_value=_FakeSession([])):
        report = await build_daily_eod_report(target_date=date(2026, 9, 27))

    assert "No alerts were sent today." in report
    assert "Powered by WhaleAlpha" in report
    assert "Powered by AlphaPulse" not in report


@pytest.mark.asyncio
async def test_eod_report_existing_counts_unchanged():
    # 1.5 -> +50% (performing), 0.8 -> -20% (non-performing), 1.0 -> 0% (performing, pct >= 0)
    rows = [_fake_signal(1.5), _fake_signal(0.8), _fake_signal(1.0)]
    with patch("domain.signals.signal_tracker.async_session", return_value=_FakeSession(rows)):
        report = await build_daily_eod_report(target_date=date(2026, 9, 27))

    assert "Total Alerts Sent: 3" in report
    assert "Performing: 2" in report
    assert "Non-Performing: 1" in report


@pytest.mark.asyncio
async def test_eod_report_total_pct_gained_is_the_sum_of_individual_pct():
    # +50% and -20% and +0% -> total = 30%
    rows = [_fake_signal(1.5), _fake_signal(0.8), _fake_signal(1.0)]
    with patch("domain.signals.signal_tracker.async_session", return_value=_FakeSession(rows)):
        report = await build_daily_eod_report(target_date=date(2026, 9, 27))

    assert "Total % Gained: +30%" in report


@pytest.mark.asyncio
async def test_eod_report_win_rate_is_performing_over_total():
    # 2 performing out of 3 total -> 66.666...% -> rounds to 67%
    rows = [_fake_signal(1.5), _fake_signal(0.8), _fake_signal(1.0)]
    with patch("domain.signals.signal_tracker.async_session", return_value=_FakeSession(rows)):
        report = await build_daily_eod_report(target_date=date(2026, 9, 27))

    assert "Win Rate: 67%" in report


@pytest.mark.asyncio
async def test_eod_report_negative_total_pct_gained_has_no_double_sign():
    # -50% and -80% -> total = -130%, must render as "-130%" not "+-130%"
    rows = [_fake_signal(0.5), _fake_signal(0.2)]
    with patch("domain.signals.signal_tracker.async_session", return_value=_FakeSession(rows)):
        report = await build_daily_eod_report(target_date=date(2026, 9, 27))

    assert "Total % Gained: -130%" in report


@pytest.mark.asyncio
async def test_eod_report_new_lines_appear_before_footer():
    rows = [_fake_signal(1.5)]
    with patch("domain.signals.signal_tracker.async_session", return_value=_FakeSession(rows)):
        report = await build_daily_eod_report(target_date=date(2026, 9, 27))

    gained_idx = report.index("Total % Gained")
    win_rate_idx = report.index("Win Rate")
    footer_idx = report.index("Powered by WhaleAlpha")
    assert gained_idx < win_rate_idx < footer_idx
