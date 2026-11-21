"""Seed lists: [3, 7], "1-5,42", "random:N"."""

import random

MAX_SEED = 2**31 - 1  # 种子取值范围 0 ~ MAX_SEED（各家仿真器都能接受的 32 位有符号正数）
MAX_SEEDS = 10_000  # 一次最多展开出多少个种子

_RANDOM_PREFIX = "random:"


def parse_seeds(spec: str | list[int], rng: random.Random | None = None) -> list[int]:
    """Return a de-duplicated seed list, keeping first-seen order."""
    # 裸整数 3 是刻意拒绝的：分不清是"种子 3"还是"3 个种子"
    if isinstance(spec, list):
        seeds = _check_list(spec)
    elif isinstance(spec, str):
        text = spec.strip()
        if text.startswith(_RANDOM_PREFIX):
            return _random_seeds(text.removeprefix(_RANDOM_PREFIX), spec, rng)
        seeds = _expand_ranges(text, spec)
    else:
        raise TypeError(f"invalid seeds {spec!r}: expected a list like [3] or a string like '1-5'")
    return list(dict.fromkeys(seeds))


def _check_list(spec: list[int]) -> list[int]:
    # bool 是 int 的子类，必须单独排除
    if any(isinstance(s, bool) or not isinstance(s, int) for s in spec):
        raise TypeError(f"invalid seeds {spec!r}: every item must be an int")
    if not spec:
        raise ValueError(f"invalid seeds {spec!r}: list is empty")
    if any(not 0 <= s <= MAX_SEED for s in spec):
        raise ValueError(f"invalid seeds {spec!r}: each seed must be in 0..{MAX_SEED}")
    return spec


def _expand_ranges(text: str, spec: str) -> list[int]:
    ranges: list[range] = []
    for part in text.split(","):
        lo_text, sep, hi_text = part.partition("-")
        lo = _parse_int(lo_text, spec)
        hi = _parse_int(hi_text, spec) if sep else lo
        if lo > hi:
            raise ValueError(f"invalid seeds {spec!r}: range {part.strip()!r} is reversed")
        ranges.append(range(lo, hi + 1))

    # range 是懒的，len() 不会真的生成数字；先数清楚再展开
    _check_count(sum(len(r) for r in ranges), spec)
    return [seed for r in ranges for seed in r]


def _random_seeds(n_text: str, spec: str, rng: random.Random | None) -> list[int]:
    n = _parse_int(n_text, spec)
    if n < 1:
        raise ValueError(f"invalid seeds {spec!r}: need at least 1 random seed")
    _check_count(n, spec)
    if rng is None:
        rng = random.Random()
    # sample 保证互不重复；对 range 取样不会先展开它
    return rng.sample(range(1, MAX_SEED + 1), n)


def _parse_int(text: str, spec: str) -> int:
    digits = text.strip()
    # 只收 ASCII 0-9：int() 还会接受 "+3"、"1_000"、"٣"、全角 "３"
    if not (digits.isascii() and digits.isdecimal()):
        raise ValueError(f"invalid seeds {spec!r}: {digits!r} is not a non-negative integer")
    value = int(digits)
    if value > MAX_SEED:
        raise ValueError(f"invalid seeds {spec!r}: {value} exceeds the maximum seed {MAX_SEED}")
    return value


def _check_count(count: int, spec: str) -> None:
    if count > MAX_SEEDS:
        raise ValueError(f"invalid seeds {spec!r}: too many seeds ({count} > {MAX_SEEDS})")
