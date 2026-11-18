"""Serializer tests."""

from __future__ import annotations

import math

import pytest

from hermes_cache import (
    JSONSerializer,
    PickleSerializer,
    RawSerializer,
    SerializationError,
)


def test_json_roundtrip():
    serializer = JSONSerializer()
    value = {"a": [1, 2, 3], "b": None, "c": "hé"}
    assert serializer.loads(serializer.dumps(value)) == value


def test_json_rejects_bytes():
    with pytest.raises(SerializationError):
        JSONSerializer().dumps(b"bytes")


def test_json_rejects_nan():
    with pytest.raises(SerializationError):
        JSONSerializer().dumps({"x": math.nan})


def test_pickle_roundtrip_arbitrary_object():
    serializer = PickleSerializer()
    value = {"set": {1, 2}, "bytes": b"x"}
    assert serializer.loads(serializer.dumps(value)) == value


def test_pickle_rejects_corrupt_payload():
    with pytest.raises(SerializationError):
        PickleSerializer().loads(b"not-pickle")


def test_raw_roundtrip():
    serializer = RawSerializer()
    assert serializer.loads(serializer.dumps(b"abc")) == b"abc"
    assert serializer.loads(serializer.dumps(bytearray(b"abc"))) == b"abc"


def test_raw_rejects_text():
    with pytest.raises(SerializationError):
        RawSerializer().dumps("text")
