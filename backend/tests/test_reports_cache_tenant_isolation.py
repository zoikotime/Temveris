"""B1 — report cache must never serve one tenant's data to another.

The defect
----------
``_RPT_CACHE`` keys were built from the query params alone::

    _ck = f"daily-attendance:{date}:{user_id}"
    _ck = f"apps-sites:{date}:{user_id}"

Both endpoints depend on ``get_current_organization`` and compute org-scoped
data correctly, but the cache is consulted BEFORE any of that filtering. So org
A's admin opening a report seeded an entry that org B's admin then received for
the remaining TTL. ``user_id`` defaults to None, which made the colliding key
the DEFAULT request rather than an edge case.

These tests exercise the cache layer directly rather than through HTTP. The
leak lives in the key, and asserting on the key is what actually pins it — a
full request test would also depend on report aggregation, seeded activity and
clock sessions, which have nothing to do with the vulnerability and would make
a failure ambiguous.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.reports import router as rpt


@pytest.fixture(autouse=True)
def _clear_cache():
    """The cache is module-level; leaking entries between tests would mask
    exactly the class of bug under test."""
    rpt._RPT_CACHE.clear()
    yield
    rpt._RPT_CACHE.clear()


ORG_A, ORG_B = 101, 202
DATE = "2026-08-07"
KEY = f"daily-attendance:{DATE}:None"          # user_id omitted -> the default


def test_org_a_entry_is_created_under_org_a(_clear_cache=None):
    rpt._rpt_cache_set(ORG_A, KEY, {"owner": "A"})
    assert rpt._rpt_cache_get(ORG_A, KEY) == {"owner": "A"}


def test_org_b_cannot_receive_org_a_cached_result():
    """The vulnerability itself."""
    rpt._rpt_cache_set(ORG_A, KEY, {"owner": "A"})
    assert rpt._rpt_cache_get(ORG_B, KEY) is None, "org B received org A's cached report"


def test_org_b_creates_and_reads_its_own_entry():
    rpt._rpt_cache_set(ORG_A, KEY, {"owner": "A"})
    rpt._rpt_cache_set(ORG_B, KEY, {"owner": "B"})
    assert rpt._rpt_cache_get(ORG_A, KEY) == {"owner": "A"}
    assert rpt._rpt_cache_get(ORG_B, KEY) == {"owner": "B"}


def test_same_user_and_date_in_two_orgs_are_separate_entries():
    """A user id is only unique within a tenant, so the same (date, user_id)
    pair legitimately occurs in both orgs."""
    key = f"daily-attendance:{DATE}:77"
    rpt._rpt_cache_set(ORG_A, key, {"owner": "A"})
    rpt._rpt_cache_set(ORG_B, key, {"owner": "B"})
    assert rpt._rpt_cache_get(ORG_A, key) == {"owner": "A"}
    assert rpt._rpt_cache_get(ORG_B, key) == {"owner": "B"}
    assert len(rpt._RPT_CACHE) == 2, "the two tenants shared one slot"


def test_user_id_none_is_isolated_by_organization():
    """`user_id=None` is the unfiltered, most common request — the exact key
    that collided in production."""
    rpt._rpt_cache_set(ORG_A, KEY, {"owner": "A"})
    assert rpt._rpt_cache_get(ORG_B, KEY) is None
    rpt._rpt_cache_set(ORG_B, KEY, {"owner": "B"})
    assert rpt._rpt_cache_get(ORG_A, KEY) == {"owner": "A"}


def test_apps_sites_key_is_isolated_too():
    """The second affected endpoint. Same shape, same defect."""
    key = f"apps-sites:{DATE}:None"
    rpt._rpt_cache_set(ORG_A, key, {"rows": ["A"]})
    assert rpt._rpt_cache_get(ORG_B, key) is None


# ── the key itself ──────────────────────────────────────────────────────────

def test_stored_key_contains_the_organization():
    rpt._rpt_cache_set(ORG_A, KEY, {"owner": "A"})
    stored = next(iter(rpt._RPT_CACHE))
    assert str(ORG_A) in stored
    assert stored.startswith("org:"), \
        "tenant must lead the key so a prefix scan cannot match another org"


@pytest.mark.parametrize("a,b", [
    (1, 2), (1, 11), (11, 1), (0, None), (None, 0),
])
def test_keys_cannot_collide_across_organizations(a, b):
    """Guards against a delimiter-free concatenation, where org 1 + key "1:x"
    and org 11 + key ":x" would produce the same string.

    `("1", 1)` is deliberately NOT a case here: org_id always arrives as
    `current_org.id`, and "1" and 1 denote the SAME tenant, so sharing a key is
    correct rather than a collision.
    """
    assert rpt._rpt_key(a, KEY) != rpt._rpt_key(b, KEY)


def test_key_builder_is_shared_by_get_and_set():
    """If get and set ever build the key differently, every lookup silently
    misses and the cache degrades to useless — or worse, drifts back."""
    rpt._RPT_CACHE[rpt._rpt_key(ORG_A, KEY)] = {
        "data": {"owner": "A"},
        "expires_at": datetime.now(timezone.utc) + timedelta(seconds=30),
    }
    assert rpt._rpt_cache_get(ORG_A, KEY) == {"owner": "A"}


def test_organization_is_required_not_optional():
    """A forgotten tenant must be a TypeError, not a silent global entry.
    That is what turned this from a bug into a data leak."""
    with pytest.raises(TypeError):
        rpt._rpt_cache_get(KEY)          # type: ignore[call-arg]
    with pytest.raises(TypeError):
        rpt._rpt_cache_set(KEY, {"x": 1})  # type: ignore[call-arg]


# ── existing behaviour preserved ────────────────────────────────────────────

def test_ttl_and_expiry_still_work():
    rpt._rpt_cache_set(ORG_A, KEY, {"owner": "A"}, ttl_seconds=-1)
    assert rpt._rpt_cache_get(ORG_A, KEY) is None, "expired entry was served"


def test_default_ttl_unchanged():
    assert rpt._RPT_TTL_SECONDS == 30


def test_prune_still_bounds_the_cache():
    """The >256 opportunistic prune must survive the key change."""
    for i in range(300):
        rpt._rpt_cache_set(ORG_A, f"k{i}", {"i": i}, ttl_seconds=-1)
    rpt._rpt_cache_set(ORG_A, "live", {"i": "live"})
    assert len(rpt._RPT_CACHE) < 300
    assert rpt._rpt_cache_get(ORG_A, "live") == {"i": "live"}
