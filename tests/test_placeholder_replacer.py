from __future__ import annotations

from pathlib import Path

import pytest
from ECL.minecraft.game import LaunchConfig
from ECL.minecraft.game.Core.ECLauncherCore import PlaceholderReplacer
from ECL.minecraft.game.Core.LaunchCommand import parse_launch_command


def _build_replacer(
    game_path: Path,
    version_isolation: bool,
    version_name: str = "1.21.8-Fabric",
    cp_delimiter: str = ";",
) -> PlaceholderReplacer:
    config = LaunchConfig(
        java_path="java",
        game_path=game_path,
        version_name=version_name,
        use_ram=4096,
        player_name="Steve",
        auth_uuid="5627dd98e6be3c21b8a8e92344183641",
        version_isolation=version_isolation,
    )
    return PlaceholderReplacer(
        config=config,
        classpath="client.jar",
        main_class="net.minecraft.client.main.Main",
        index_id="1.21",
        natives_dir=game_path / "natives",
        version_jar_path=game_path / "client.jar",
        cp_delimiter=cp_delimiter,
    )


@pytest.mark.parametrize("version_isolation", [True, False])
def test_game_directory_follows_config_version_isolation(tmp_path: Path, version_isolation: bool) -> None:
    """启动命令中的 gameDir 必须与 LaunchConfig 的隔离状态一致。"""
    replacer = _build_replacer(tmp_path, version_isolation)

    command = replacer.replace("--gameDir ${game_directory}")

    expected = tmp_path / "versions" / "1.21.8-Fabric" if version_isolation else tmp_path / "versions"
    assert command == f'--gameDir "{expected}"'


def test_isolated_game_directory_keeps_version_name(tmp_path: Path) -> None:
    """隔离开启时数据目录应落在实例自己的 versions/<版本名>/ 下。"""
    replacer = _build_replacer(tmp_path, version_isolation=True)

    replacements = replacer._build_standard_replacements()

    assert replacements["game_directory"] == str(tmp_path / "versions" / "1.21.8-Fabric")


def test_version_name_is_quoted_in_every_placeholder_position(tmp_path: Path) -> None:
    """版本名出现在忽略列表和游戏参数时都必须作为完整值引用。"""
    version_name = "Zombie Invade 100 Days"
    replacer = _build_replacer(tmp_path, version_isolation=False, version_name=version_name)

    command = replacer.replace("-DignoreList=forge-,${version_name}.jar --version ${version_name} -cp ${classpath}")

    assert command == (
        f'-DignoreList=forge-,"{version_name}".jar '
        f'--version "{version_name}" '
        f'-cp "client.jar" "net.minecraft.client.main.Main"'
    )


def test_classpath_separator_is_bare_between_quoted_segments(tmp_path: Path) -> None:
    """分隔符裸替换后，Forge 模块路径在解析后仍是单个参数，主类保持独立 token。"""
    replacer = _build_replacer(tmp_path, version_isolation=False)
    libraries = str(tmp_path / "libraries")

    command = replacer.replace(
        "-p ${library_directory}/a.jar${classpath_separator}${library_directory}/b.jar -cp ${classpath}"
    )

    argv = parse_launch_command(command)
    assert argv == [
        "-p",
        f"{libraries}/a.jar;{libraries}/b.jar",
        "-cp",
        "client.jar",
        "net.minecraft.client.main.Main",
    ]


def test_numeric_and_index_placeholders_are_bare(tmp_path: Path) -> None:
    """索引名与分辨率是结构值，替换结果不携带引号。"""
    replacer = _build_replacer(tmp_path, version_isolation=False)

    command = replacer.replace(
        "--assetIndex ${assets_index_name} --width ${resolution_width} --height ${resolution_height}"
    )

    assert command == "--assetIndex 1.21 --width 854 --height 480"


def test_paths_and_names_stay_quoted(tmp_path: Path) -> None:
    """可含空格的目录、玩家名与实例名必须保持引用，守护既有行为。"""
    version_name = "Zombie Invade 100 Days"
    replacer = _build_replacer(tmp_path, version_isolation=False, version_name=version_name)

    command = replacer.replace("--gameDir ${game_directory} --username ${auth_player_name} --version ${version_name}")

    assert command == (f'--gameDir "{tmp_path / "versions"}" --username "Steve" --version "{version_name}"')
