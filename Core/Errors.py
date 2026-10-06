# ============================================================
# EuoraCraft Launcher Core
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher.Core
#
# 文件作用：Core 领域异常基类与 Minecraft 本地数据原语异常。
#
# 公开接口：
#   - class CoreError — Core 各领域异常的公共基类。
#   - class GameDataError — Minecraft 与实例本地数据原语的领域异常。
# ============================================================

from __future__ import annotations


class CoreError(Exception):
    """
    Core 各领域异常的公共基类。

    ``error_code`` 为可选属性：仅在显式传入时才设置。调用方普遍以
    ``getattr(exc, "error_code", 默认码)`` 读取错误码，因此未传入错误码的
    子类不会凭空获得该属性，也不会改变既有异常到外部错误码的映射结果。
    需要稳定错误码的领域异常应在构造时显式传入，或在子类中给出默认值。

    :param message: 面向调用方的错误说明
    :param error_code: 供上层转换为稳定错误码的标识；省略时不设置该属性
    """

    def __init__(self, message: str, error_code: str | None = None) -> None:
        super().__init__(message)
        if error_code is not None:
            self.error_code = error_code


class GameDataError(CoreError):
    """
    Minecraft 与实例本地数据原语的领域异常。

    语义与主仓库的游戏服务异常一致：携带供上层转换为稳定错误码的
    ``error_code``，使 IPC 边界能在不改变既有错误码的前提下识别数据层失败。
    原语始终显式传入错误码，因此未传入时不设置该属性，交由调用方回退。

    :param message: 面向用户的错误说明
    :param error_code: 供前端识别的稳定错误码
    """


__all__ = ["CoreError", "GameDataError"]
