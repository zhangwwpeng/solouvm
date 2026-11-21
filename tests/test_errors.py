import json
from pathlib import Path

import pytest

from solouvm.errors import (
    ERROR_CODES,
    BuildError,
    ConfigError,
    InfraError,
    RecordError,
    RoleError,
    RunActiveError,
    SolouvmError,
    format_error,
)

# ---------- 错误码表 ----------


def test_error_codes_table():
    assert ERROR_CODES["CFG_NOT_FOUND"] == 2
    assert ERROR_CODES["TESTS_FAILED"] == 1
    assert ERROR_CODES["INTERRUPTED"] == 130
    assert len(ERROR_CODES) == 29  # formats.md §9 的全部错误码


# ---------- 继承关系与默认错误码 ----------


@pytest.mark.parametrize(
    ("cls", "code"),
    [
        (ConfigError, "CFG_INVALID"),
        (InfraError, "PROVIDER_FAILED"),
        (BuildError, "BUILD_FAILED"),
        (RoleError, "ROLE_DENIED"),
        (RunActiveError, "RUN_ACTIVE"),
        (RecordError, "RECORD_INVALID"),
    ],
)
def test_default_code(cls, code):
    err = cls("boom")
    assert err.code == code
    assert err.exit_code == 2
    assert isinstance(err, SolouvmError)
    assert isinstance(err, Exception)


def test_every_subclass_has_a_known_code():
    # 新增子类时忘了配错误码，这条测试会提醒你
    for cls in SolouvmError.__subclasses__():
        assert cls.code in ERROR_CODES, cls.__name__


def test_catch_by_base_class():
    with pytest.raises(SolouvmError):
        raise ConfigError("bad")


# ---------- 构造参数 ----------


def test_defaults():
    err = ConfigError("bad value")
    assert err.message == "bad value"
    assert err.hint is None
    assert err.file is None
    assert err.line is None


def test_override_code():
    err = ConfigError("unknown key 'sedes'", code="CFG_UNKNOWN_KEY", hint="did you mean 'seeds'?")
    assert err.code == "CFG_UNKNOWN_KEY"
    assert err.hint == "did you mean 'seeds'?"


def test_override_code_does_not_change_class_default():
    ConfigError("a", code="CFG_UNKNOWN_KEY")
    assert ConfigError("b").code == "CFG_INVALID"
    assert ConfigError.code == "CFG_INVALID"


def test_unknown_code_rejected():
    # 错误码只增不改，拼错了要立刻发现
    with pytest.raises(ValueError, match="CFG_TYPO"):
        ConfigError("bad", code="CFG_TYPO")


def test_options_are_keyword_only():
    # hint、code、file、line 只能用关键字传，避免位置传错
    with pytest.raises(TypeError):
        ConfigError("bad", "some hint")  # type: ignore


def test_file_accepts_path_and_is_stored_as_str():
    err = ConfigError("bad", file=Path("cfg") / "solouvm.conf.py", line=12)
    assert err.file == str(Path("cfg") / "solouvm.conf.py")
    assert isinstance(err.file, str)


def test_line_without_file_rejected():
    with pytest.raises(ValueError, match="line"):
        ConfigError("bad", line=3)


# ---------- __str__ ----------


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({}, "bad value"),
        ({"file": "solouvm.conf.py"}, "solouvm.conf.py: bad value"),
        ({"file": "solouvm.conf.py", "line": 12}, "solouvm.conf.py:12: bad value"),
        ({"hint": "try X"}, "bad value"),  # hint 不进 str()
    ],
)
def test_str(kwargs, expected):
    assert str(ConfigError("bad value", **kwargs)) == expected


# ---------- to_dict：--json 错误外壳里的 error 字段 ----------


def test_to_dict():
    err = ConfigError(
        "unknown key 'sedes'",
        code="CFG_UNKNOWN_KEY",
        hint="did you mean 'seeds'?",
        file="solouvm.conf.py",
        line=7,
    )
    assert err.to_dict() == {
        "code": "CFG_UNKNOWN_KEY",
        "message": "unknown key 'sedes'",  # 原始消息，不带文件位置
        "file": "solouvm.conf.py",
        "line": 7,
        "hint": "did you mean 'seeds'?",
    }


def test_to_dict_is_json_serializable():
    err = BuildError("compile failed", file=Path("build.log"))
    assert json.loads(json.dumps(err.to_dict()))["file"] == "build.log"


# ---------- format_error：给人看的终端输出 ----------


def test_format_error_without_hint():
    assert format_error(RoleError("command not allowed")) == "solouvm: error: command not allowed"


def test_format_error_with_hint_and_location():
    err = ConfigError(
        "unknown key 'sedes'", hint="did you mean 'seeds'?", file="solouvm.conf.py", line=7
    )
    assert format_error(err) == (
        "solouvm: error: solouvm.conf.py:7: unknown key 'sedes'\nhint: did you mean 'seeds'?"
    )


# ---------- 补充 ----------


def test_exit_code_follows_overridden_code():
    assert SolouvmError("x", code="TESTS_FAILED").exit_code == 1
    assert SolouvmError("x", code="INTERRUPTED").exit_code == 130


def test_args_set_by_super_init():
    assert ConfigError("bad").args == ("bad",)


def test_exit_code_is_read_only():
    with pytest.raises(AttributeError):
        ConfigError("bad").exit_code = 1  # type: ignore[misc]
