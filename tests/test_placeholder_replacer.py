from __future__ import annotations

from pathlib import Path

import pytest
from ECL.game import LaunchConfig
from ECL.game.Core.ECLauncherCore import PlaceholderReplacer


def _build_replacer(game_path: Path, version_isolation: bool) -> PlaceholderReplacer:
    config = LaunchConfig(
        java_path="java",
        game_path=game_path,
        version_name="1.21.8-Fabric",
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
