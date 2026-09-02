from whale_alpha.engines.price_alerts import MILESTONE_THRESHOLDS, crossed_milestones


def test_milestone_thresholds_are_25_50_75_2x_through_5x():
    assert [pct for pct, _ in MILESTONE_THRESHOLDS] == [25, 50, 75, 100, 200, 300, 400]


def test_crossed_milestones_returns_all_new_thresholds():
    assert crossed_milestones(210, set()) == [25, 50, 75, 100, 200]


def test_crossed_milestones_never_repeats_sent_thresholds():
    assert crossed_milestones(500, {25, 50, 75, 100, 200, 300, 400}) == []
    assert crossed_milestones(300, {25, 50, 75, 100}) == [200, 300]
