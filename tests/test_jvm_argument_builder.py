from __future__ import annotations

from ECL.minecraft.game.Core.ECLauncherCore import JvmArgumentBuilder


def _build_builder() -> JvmArgumentBuilder:
    return JvmArgumentBuilder(java_path="java", version_name="Zombie Invade 100 Days", use_ram=3840)


def test_repeated_option_flags_keep_their_values() -> None:
    """同一选项伴随不同值重复出现时必须成对保留，值不得脱离标志裸露。"""
    builder = _build_builder()
    builder.add_jvm_args(
        {
            "arguments": {
                "jvm": [
                    "--add-opens",
                    "java.base/java.util.jar=cpw.mods.securejarhandler",
                    "--add-opens",
                    "java.base/java.lang.invoke=cpw.mods.securejarhandler",
                    "--add-exports",
                    "java.base/sun.security.util=cpw.mods.securejarhandler",
                    "--add-exports",
                    "jdk.naming.dns/com.sun.jndi.dns=java.naming",
                ]
            }
        }
    )

    args = builder.get_args()
    assert args.count("--add-opens") == 2
    assert args.count("--add-exports") == 2

    first_opens = args.index("--add-opens")
    assert args[first_opens + 1] == "java.base/java.util.jar=cpw.mods.securejarhandler"
    second_opens = args.index("--add-opens", first_opens + 1)
    assert args[second_opens + 1] == "java.base/java.lang.invoke=cpw.mods.securejarhandler"


def test_child_and_parent_arguments_merge_in_order() -> None:
    """子版本与父版本的参数按调用顺序合并，-cp 与 classpath 保持配对。"""
    builder = _build_builder()
    builder.add_jvm_args(
        {
            "arguments": {
                "jvm": [
                    "-DignoreList=forge-,${version_name}.jar",
                    "-p",
                    "${library_directory}/cpw/a.jar${classpath_separator}${library_directory}/cpw/b.jar",
                ]
            }
        }
    )
    builder.add_jvm_args(
        {
            "arguments": {
                "jvm": [
                    "-Djava.library.path=${natives_directory}",
                    "-cp",
                    "${classpath}",
                ]
            }
        }
    )

    args = builder.get_args()
    assert args[args.index("-cp") + 1] == "${classpath}"
    module_path = args[args.index("-p") + 1]
    assert module_path == "${library_directory}/cpw/a.jar${classpath_separator}${library_directory}/cpw/b.jar"


def test_repeated_game_arguments_keep_their_values() -> None:
    """游戏参数同样按原始顺序保留，不去重。"""
    builder = _build_builder()
    builder.add_game_args(
        {
            "arguments": {
                "game": [
                    "--username",
                    "${auth_player_name}",
                    "--launchTarget",
                    "forgeclient",
                ]
            }
        }
    )

    args = builder.get_args()
    assert args[args.index("--username") + 1] == "${auth_player_name}"
    assert args[args.index("--launchTarget") + 1] == "forgeclient"


def test_identical_pairs_from_child_and_parent_are_deduplicated() -> None:
    """导入实例会把基础版本元数据复制进子版本描述，相同的 (标志, 值) 对收敛为一份。"""
    builder = _build_builder()
    vanilla = {
        "arguments": {
            "jvm": ["-cp", "${classpath}"],
            "game": ["--username", "${auth_player_name}"],
        }
    }
    builder.add_jvm_args(vanilla)
    builder.add_jvm_args(vanilla)
    builder.add_game_args(vanilla)

    args = builder.get_args()
    assert args.count("-cp") == 1
    assert args.count("${classpath}") == 1
    assert args.count("--username") == 1
