from __future__ import annotations

import pytest
from ECL.minecraft.game.Core.Errors import CoreError
from ECL.minecraft.game.Core.LaunchCommand import parse_launch_command, validate_launch_command

_MAIN_CLASS = "cpw.mods.bootstraplauncher.BootstrapLauncher"
_VERSION_NAME = "Zombie Invade 100 Days"


def _forge_command(*, quote_version_name: bool, quote_main_class: bool = True) -> str:
    ignore_value = f'...,forge-,"{_VERSION_NAME}".jar' if quote_version_name else f"...,forge-,{_VERSION_NAME}.jar"
    version_value = f'"{_VERSION_NAME}"'
    main_value = f'"{_MAIN_CLASS}"' if quote_main_class else _MAIN_CLASS
    return (
        '"java.exe" -Xms256M -Xmx4096M '
        f"-DignoreList={ignore_value} "
        f'-cp "client.jar" {main_value} '
        f"--version {version_value}"
    )


def test_parse_launch_command_preserves_quoted_token() -> None:
    command = _forge_command(quote_version_name=True)

    argv = parse_launch_command(command)

    assert any(token.startswith("-DignoreList=") and f"{_VERSION_NAME}.jar" in token for token in argv)
    assert "client.jar" in argv


def test_validate_launch_command_accepts_space_version_name() -> None:
    command = _forge_command(quote_version_name=True)

    validate_launch_command(command, version_name=_VERSION_NAME, main_class=_MAIN_CLASS)

    argv = parse_launch_command(command)
    assert argv.count(_MAIN_CLASS) == 1


def test_validate_launch_command_rejects_split_version_name() -> None:
    command = _forge_command(quote_version_name=False)

    with pytest.raises(CoreError) as exc_info:
        validate_launch_command(command, version_name=_VERSION_NAME, main_class=_MAIN_CLASS)

    assert exc_info.value.error_code == "LAUNCH_COMMAND_INVALID"
    assert "实例名称" in str(exc_info.value)


def test_validate_launch_command_rejects_split_or_missing_main_class() -> None:
    command = _forge_command(quote_version_name=True, quote_main_class=False).replace(_MAIN_CLASS, "Invade", 1)

    with pytest.raises(CoreError) as exc_info:
        validate_launch_command(command, version_name=_VERSION_NAME, main_class=_MAIN_CLASS)

    assert exc_info.value.error_code == "LAUNCH_COMMAND_INVALID"
    assert "主类" in str(exc_info.value)


def test_validate_launch_command_rejects_invalid_main_class() -> None:
    with pytest.raises(CoreError) as exc_info:
        validate_launch_command(
            _forge_command(quote_version_name=True),
            version_name=_VERSION_NAME,
            main_class="cpw.mods .BootstrapLauncher",
        )

    assert exc_info.value.error_code == "LAUNCH_COMMAND_INVALID"
    assert "非法字符" in str(exc_info.value)


def test_validate_launch_command_rejects_empty_version_name() -> None:
    with pytest.raises(CoreError) as exc_info:
        validate_launch_command(_forge_command(quote_version_name=True), version_name="   ", main_class=_MAIN_CLASS)

    assert exc_info.value.error_code == "LAUNCH_COMMAND_INVALID"
    assert "实例名称" in str(exc_info.value)


def test_validate_accepts_realistic_forge_module_path() -> None:
    """真实 Forge 命令形态（模块路径夹在引号段之间）必须通过校验。"""
    libs = "D:/game/libraries"
    versions = "D:/game/versions/Zombie Invade 100 Days"
    command = (
        '"java.exe" -Xms256M -Xmx4096M '
        f'-DignoreList=bootstraplauncher,forge-,"{_VERSION_NAME}.jar" '
        f'-Djava.library.path="{versions}/natives" '
        f'-p "{libs}/cpw/a.jar;{libs}/cpw/b.jar" '
        f'-cp "{libs}/client-extra.jar;{versions}/1.20.1.jar" {_MAIN_CLASS} '
        f'--username Steve --version "{_VERSION_NAME}" '
        f'--gameDir "{versions}"'
    )

    validate_launch_command(command, version_name=_VERSION_NAME, main_class=_MAIN_CLASS)

    argv = parse_launch_command(command)
    assert argv.count(_MAIN_CLASS) == 1
    module_path_tokens = [token for token in argv if token.endswith("b.jar")]
    assert len(module_path_tokens) == 1
    assert f"{libs}/cpw/a.jar;{libs}/cpw/b.jar" == module_path_tokens[0]


def test_validate_rejects_quoted_classpath_separator_corruption() -> None:
    """分隔符被加引号时 `";""` 序列破坏引号状态，主类被合并进巨型 token，必须被校验拦截。"""
    libs = "D:/game/libraries"
    # 旧行为：classpath_separator 替换为 `";"`，与相邻的引号段构成 `";""`
    command = (
        f'"{libs}/a.jar";""{libs}"/b.jar";""{libs}"/c.jar -cp "client.jar" {_MAIN_CLASS} --version "{_VERSION_NAME}"'
    )

    with pytest.raises(CoreError) as exc_info:
        validate_launch_command(command, version_name=_VERSION_NAME, main_class=_MAIN_CLASS)

    assert exc_info.value.error_code == "LAUNCH_COMMAND_INVALID"
    assert "主类" in str(exc_info.value)
