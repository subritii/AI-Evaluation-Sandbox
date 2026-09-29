"""Tests for latency summaries (no DB needed)."""

import numpy as np

from app.metrics.latency import summarize


def test_summarize_matches_numpy_percentiles():
    values = list(range(1, 201))  # 1..200 ms
    s = summarize(values)
    assert s["n"] == 200
    assert s["avg"] == 100.5
    for p in (50, 90, 95, 99):
        assert s[f"p{p}"] == round(float(np.percentile(values, p)), 2)
    assert (s["min"], s["max"]) == (1, 200)


def test_one_slow_request_moves_p99_but_not_p50():
    """Why sample size matters: at n=100, a single outlier drags P99 but leaves P50 alone."""
    fast = [100.0] * 99
    base, spiked = summarize(fast + [100.0]), summarize(fast + [5000.0])
    assert base["p50"] == spiked["p50"] == 100.0
    assert spiked["p99"] > base["p99"] + 40
