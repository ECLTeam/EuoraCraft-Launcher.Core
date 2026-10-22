from __future__ import annotations

from pathlib import Path

from ECL.game import LaunchConfig


def _build_config(**overrides: object) -> LaunchConfig:
    values: dict[str, object] = {
        "java_path": "java",
        "game_path": Path("/tmp/.minecraft"),
        "version_name": "1.21.8",
        "use_ram": 2048,
        "player_name": "Steve",
        "auth_uuid": "5627dd98e6be3c21b8a8e92344183641",
    }
    values.update(overrides)
    return LaunchConfig(**values)


def test_authlib_path_defaults_to_none() -> None:
    """外置登录未配置时默认值必须是 None，而不是写成 ``= None,`` 产生的元组。"""
    config = _build_config()

    assert config.authlib_path is None
    assert config.yggdrasil_api is None


def test_authlib_path_keeps_explicit_value() -> None:
    config = _build_config(authlib_path=Path("/tmp/authlib-injector.jar"))

    assert config.authlib_path == Path("/tmp/authlib-injector.jar")
