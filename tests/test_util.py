from __future__ import annotations

from pathlib import Path

import pytest

from solouvm.errors import ConfigError
from solouvm.util import (
    case_id,
    new_run_id,
    parse_duration,
    parse_seeds,
    read_json,
    relpath,
    slug,
    write_json_atomic,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("500ms", 0.5),
        ("60s", 60.0),
        ("20m", 1200.0),
        ("1.5h", 5400.0),
        ("1h30m", 5400.0),
        ("2d", 172800.0),
        (30, 30.0),
        (0.25, 0.25),
    ],
)
def test_parse_duration(text, expected):
    assert parse_duration(text) == pytest.approx(expected)


@pytest.mark.parametrize("text", ["", "abc", "10", "10x", "m", "-5s", 0, -1])
def test_parse_duration_rejects_garbage(text):
    with pytest.raises(ConfigError):
        parse_duration(text)


def test_parse_seeds_range_and_list():
    assert parse_seeds("1-3,10") == [1, 2, 3, 10]
    assert parse_seeds("5,5,7") == [5, 7]
    assert parse_seeds([3, 1, 3]) == [3, 1]
    assert parse_seeds([]) == []
    assert parse_seeds(0) == []


def test_parse_seeds_random_is_unique_and_stable_in_size():
    seeds = parse_seeds(5)
    assert len(seeds) == 5
    assert len(set(seeds)) == 5


@pytest.mark.parametrize("bad", ["3-1", "a-b", True, {"seeds": 1}])
def test_parse_seeds_rejects_garbage(bad):
    with pytest.raises(ConfigError):
        parse_seeds(bad)


def test_slug_and_case_id():
    assert slug("ahb smoke / test") == "ahb_smoke_test"
    assert slug("...") == "case"
    assert case_id("ahb smoke", 7) == "ahb_smoke__7"


def test_atomic_json_roundtrip(tmp_path: Path):
    target = tmp_path / "nested" / "payload.json"
    write_json_atomic(target, {"a": 1, "b": [1, 2]})
    assert read_json(target) == {"a": 1, "b": [1, 2]}
    assert not list(target.parent.glob("*.tmp"))


def test_relpath_falls_back_to_absolute(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "other" / "f.sv"
    assert relpath(root / "a.sv", root) == "a.sv"
    assert relpath(outside, root) == str(outside.resolve())


def test_new_run_id_is_unique():
    ids = {new_run_id() for _ in range(20)}
    assert len(ids) == 20
