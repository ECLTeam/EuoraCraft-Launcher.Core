# ============================================================
# EuoraCraft Launcher Core
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher.Core
#
# 文件作用：按平台规则解析最终启动命令，并校验版本名与主类没有被参数切分。
#
# 公开接口：
#   - parse_launch_command(command) -> list[str] — 按当前平台规则解析命令行参数。
#   - validate_launch_command(command, *, version_name, main_class) -> None — 校验启动命令中的版本名与主类边界。
# ============================================================

from __future__ import annotations

import ctypes
import platform
import shlex

from .Errors import CoreError


def parse_launch_command(command: str) -> list[str]:
    """
    按当前平台规则解析启动命令，返回实际传给进程的参数列表。

    Windows 使用系统命令行解析规则，保留引号内空格；其他平台使用 POSIX
    shell 规则。调用方传入的是已经完成占位符替换的最终命令字符串。

    :param command: 已完成占位符替换的启动命令
    :return: 解析后的参数列表
    :raises CoreError: 命令为空或平台解析失败时抛出
    """
    if not command.strip():
        raise CoreError("启动命令为空，无法校验启动参数", "LAUNCH_COMMAND_INVALID")
    if platform.system() == "Windows":
        return _parse_windows_command(command)
    try:
        return shlex.split(command, posix=True)
    except ValueError as exc:
        raise CoreError("启动命令引号不配对，无法解析启动参数", "LAUNCH_COMMAND_INVALID") from exc


def validate_launch_command(command: str, *, version_name: str, main_class: str) -> None:
    """
    校验最终启动命令中的版本名与主类没有被空格或引号切分。

    版本名会出现在类路径、游戏参数和 Forge/NeoForge 的忽略列表中；只要某个
    出现位置被命令行解析拆成多个 token，解析后的完整出现次数就会少于原始
    命令中的次数。主类必须是唯一的独立 token，否则 JVM 可能把其他片段当作
    主类。

    :param command: 已完成占位符替换的启动命令
    :param version_name: 当前实例目录名，也是启动参数中的版本名
    :param main_class: 版本 JSON 声明的 Java 主类
    :raises CoreError: 版本名或主类被参数切分、为空或包含非法字符时抛出
    """
    normalized_version = version_name.strip()
    if not normalized_version:
        raise CoreError("实例名称为空，无法构建启动命令", "LAUNCH_COMMAND_INVALID")
    if not main_class.strip():
        raise CoreError("版本描述缺少启动主类，无法启动游戏", "LAUNCH_COMMAND_INVALID")
    if any(character.isspace() or character in {'"', "'"} for character in main_class):
        raise CoreError(f"启动主类包含非法字符：{main_class}", "LAUNCH_COMMAND_INVALID")

    argv = parse_launch_command(command)
    main_class_count = argv.count(main_class)
    if main_class_count != 1:
        raise CoreError(
            f"启动命令中的主类未作为唯一参数出现（出现 {main_class_count} 次）：{main_class}",
            "LAUNCH_COMMAND_INVALID",
        )

    expected_version_count = command.count(normalized_version)
    parsed_version_count = sum(token.count(normalized_version) for token in argv)
    if expected_version_count != parsed_version_count:
        raise CoreError(
            "启动命令中的实例名称被空格或引号切分，请检查实例名称后重试",
            "LAUNCH_COMMAND_INVALID",
        )


def _parse_windows_command(command: str) -> list[str]:
    """
    使用 Windows 系统命令行解析规则拆分命令。

    该函数只在 Windows 运行时调用；解析结果由系统分配的缓冲区承载，必须在
    读取完成后通过 LocalFree 释放，避免每次启动泄漏内存。

    :param command: 待解析的完整命令行
    :return: 参数列表
    :raises CoreError: 系统解析失败时抛出
    """
    shell32 = ctypes.windll.shell32
    kernel32 = ctypes.windll.kernel32
    shell32.CommandLineToArgvW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.POINTER(ctypes.c_int),
    ]
    shell32.CommandLineToArgvW.restype = ctypes.POINTER(ctypes.c_wchar_p)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    argument_count = ctypes.c_int()
    argv = shell32.CommandLineToArgvW(
        ctypes.c_wchar_p(command),
        ctypes.byref(argument_count),
    )
    if not argv:
        raise CoreError("Windows 无法解析启动命令，请检查启动参数", "LAUNCH_COMMAND_INVALID")
    try:
        return [argv[index] for index in range(argument_count.value)]
    finally:
        kernel32.LocalFree(ctypes.cast(argv, ctypes.c_void_p))


__all__ = ["parse_launch_command", "validate_launch_command"]
