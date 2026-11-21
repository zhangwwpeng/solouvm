import random

import pytest

from solouvm.seeds import MAX_SEED, MAX_SEEDS, parse_seeds

# ---------- 列表：固定种子 ----------


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ([42], [42]),
        ([3, 1, 2], [3, 1, 2]),
        ([3, 1, 3, 1], [3, 1]),  # 去重，保留第一次出现的位置
        ([0, MAX_SEED], [0, MAX_SEED]),  # 边界值本身合法
    ],
)
def test_list_valid(spec, expected):
    assert parse_seeds(spec) == expected


def test_list_returns_new_list():
    # 调用方以后改返回值，不应该改到配置里的原列表
    spec = [1, 2]
    result = parse_seeds(spec)
    assert result == spec
    assert result is not spec


@pytest.mark.parametrize("spec", [[], [-1], [MAX_SEED + 1]])
def test_list_invalid_value(spec):
    with pytest.raises(ValueError) as excinfo:
        parse_seeds(spec)
    assert repr(spec) in str(excinfo.value)


@pytest.mark.parametrize("spec", [[True], ["3"], [1.0], [1, None]])
def test_list_invalid_element_type(spec):
    with pytest.raises(TypeError):
        parse_seeds(spec)


# ---------- 字符串：范围表达式 ----------


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("42", [42]),
        ("1-5", [1, 2, 3, 4, 5]),
        ("1-5,42", [1, 2, 3, 4, 5, 42]),
        ("7-7", [7]),
        (" 1 - 3 , 9 ", [1, 2, 3, 9]),  # 每一段前后的空格都忽略
        ("5,1-5", [5, 1, 2, 3, 4]),  # 去重保序
        ("3,3,3", [3]),
        ("0", [0]),
        (str(MAX_SEED), [MAX_SEED]),
    ],
)
def test_text_valid(spec, expected):
    assert parse_seeds(spec) == expected


@pytest.mark.parametrize(
    "spec",
    [
        "",
        "   ",
        "1,,2",  # 空段
        "1,",
        ",1",
        "5-1",  # 反向范围
        "-5",  # 负数
        "1-",
        "-",
        "1-2-3",
        "abc",
        "1.5",
        "+3",  # int("+3") 能转，但这里不接受
        "1_000",  # int("1_000") 能转，但这里不接受
        "٣",  # 阿拉伯-印度数字 3：isdecimal() 为 True，int() 也能转
        "²",  # isdigit() 为 True，int() 却会报错
        "٣-5",  # 范围两端也要做同样的检查
        "1-+5",
        str(MAX_SEED + 1),
    ],
)
def test_text_invalid(spec):
    with pytest.raises(ValueError) as excinfo:
        parse_seeds(spec)
    assert repr(spec) in str(excinfo.value)


# ---------- 数量上限 ----------


def test_max_seeds_boundary():
    assert len(parse_seeds(f"0-{MAX_SEEDS - 1}")) == MAX_SEEDS


@pytest.mark.parametrize("spec", [f"0-{MAX_SEEDS}", f"0-{MAX_SEED}", f"random:{MAX_SEEDS + 1}"])
def test_too_many_seeds(spec):
    # "0-2147483647" 若先展开再检查，会吃掉几十 GB 内存；必须在展开前就报错
    with pytest.raises(ValueError, match="too many"):
        parse_seeds(spec)


# ---------- 随机 ----------


def test_random_is_reproducible_with_injected_rng():
    first = parse_seeds("random:5", rng=random.Random(0))
    second = parse_seeds("random:5", rng=random.Random(0))
    assert first == second


@pytest.mark.parametrize("n", [1, 5, 100])
def test_random_shape(n):
    seeds = parse_seeds(f"random:{n}", rng=random.Random(1))
    assert len(seeds) == n
    assert len(set(seeds)) == n  # 互不相同
    assert all(type(s) is int and 1 <= s <= MAX_SEED for s in seeds)


def test_random_default_rng():
    # 不传 rng 也能用（内部自己建一个）
    assert len(parse_seeds("random:3")) == 3


def test_random_allows_spaces():
    assert len(parse_seeds(" random:3 ", rng=random.Random(0))) == 3


@pytest.mark.parametrize(
    "spec", ["random:", "random:0", "random:-1", "random:abc", "random:1.5", "random:+2", "rand:3"]
)
def test_random_invalid(spec):
    with pytest.raises(ValueError) as excinfo:
        parse_seeds(spec)
    assert repr(spec) in str(excinfo.value)


# ---------- 类型 ----------


@pytest.mark.parametrize("spec", [3, 3.0, True, None, (1, 2), {"a": 1}])
def test_invalid_type(spec):
    # 裸整数 3 是刻意拒绝的：分不清用户想要"种子 3"还是"3 个随机种子"
    with pytest.raises(TypeError):
        parse_seeds(spec)
