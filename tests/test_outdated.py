"""Tests for app/outdated.py — the relative "no longer receives updates"
rule behind the dashboard's "Hide outdated" button."""

from __future__ import annotations

import datetime as dt

from app.outdated import OUTDATED_LAG_DAYS, find_outdated

D = dt.date


def test_product_lagging_behind_its_family_is_outdated():
    # Real-world shape from 2026-11: SQL 2016's last update was in July,
    # every other SQL Server version got one in November.
    dates = {
        "sql-2025": D(2026, 11, 12),
        "sql-2022": D(2026, 11, 12),
        "sql-2019": D(2026, 11, 10),
        "sql-2017": D(2026, 11, 10),
        "sql-2016": D(2026, 7, 14),
    }
    assert find_outdated(dates) == {"sql-2016"}


def test_family_that_was_all_updated_together_is_never_outdated():
    # .NET Framework: everything got the October update, nothing newer.
    # Must not be flagged just because other families are already on November.
    dates = {"netfx-4.8.1": D(2026, 10, 13), "netfx-4.8": D(2026, 10, 13), "netfx-3.5": D(2026, 10, 13)}
    assert find_outdated(dates) == set()


def test_small_lag_within_a_patch_cycle_or_two_is_not_outdated():
    newest = D(2026, 11, 19)
    dates = {
        "preview": newest,
        "skipped-one-month": newest - dt.timedelta(days=40),
        "exactly-at-limit": newest - dt.timedelta(days=OUTDATED_LAG_DAYS),
        "just-over-limit": newest - dt.timedelta(days=OUTDATED_LAG_DAYS + 1),
    }
    assert find_outdated(dates) == {"just-over-limit"}


def test_products_without_patches_are_never_flagged():
    dates = {"new": D(2026, 11, 10), "no-data-yet": None}
    assert find_outdated(dates) == set()
    assert find_outdated({"a": None, "b": None}) == set()
    assert find_outdated({}) == set()


def test_custom_lag():
    dates = {"a": D(2026, 11, 10), "b": D(2026, 10, 1)}
    assert find_outdated(dates, lag_days=30) == {"b"}
    assert find_outdated(dates, lag_days=60) == set()
