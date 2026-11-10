"""Policy validation tests."""

from __future__ import annotations

import math
from typing import Any, cast

import pytest

from hermes_cache import CachePolicy, InvalidTTLError


def test_defaults_and_hashability():
    policy = CachePolicy()
    assert hash(policy) == hash(CachePolicy())
    assert policy.default_ttl is None
    assert policy.l1_enabled is True


def test_zero_l1_entries_allowed():
    assert CachePolicy(max_l1_entries=0).max_l1_entries == 0


def test_invalid_default_ttl():
    with pytest.raises(InvalidTTLError):
        CachePolicy(default_ttl=-1)
    with pytest.raises(InvalidTTLError):
        CachePolicy(default_ttl=math.nan)


def test_invalid_limits():
    with pytest.raises(ValueError):
        CachePolicy(max_l1_entries=-1)
    with pytest.raises(ValueError):
        CachePolicy(max_l1_entries=True)
    with pytest.raises(ValueError):
        CachePolicy(max_l1_bytes=0)
    with pytest.raises(ValueError):
        CachePolicy(l1_max_age=0)
    with pytest.raises(ValueError):
        CachePolicy(maintenance_every_writes=-1)
    with pytest.raises(ValueError):
        CachePolicy(max_l2_bytes=0)


def test_bool_fields_validated():
    with pytest.raises(ValueError):
        CachePolicy(fail_open=cast(Any, 1))
    with pytest.raises(ValueError):
        CachePolicy(raise_on_corrupt=cast(Any, 1))
