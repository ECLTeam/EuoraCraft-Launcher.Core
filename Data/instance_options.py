# ============================================================
# EuoraCraft Launcher Core
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher.Core
#
# 文件作用：实例 options.txt 的结构化读取与受限增量写入原语，不含实例解析。
#
# 公开接口：
#   - class InstanceOptionsStore — 在给定 options.txt 路径上读取可结构化编辑的键并原子写入补丁，未知行与顺序保持只读。
#       - read(options_path) -> dict[str, Any] — 仅返回可结构化编辑的常用键及其取值约束。
#       - patch(options_path, patch) -> dict[str, Any] — 按受限 schema 写入指定键，保留未知行与原有顺序。
# ============================================================

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..Core.Errors import GameDataError
from ..Core.Libs import atomic_write_text


class InstanceOptionsStore:
    """
    结构化读写实例级 options.txt，覆盖常用游戏设置，未知键保持只读。

    仅操作调用方给出的 options.txt 路径，不解析实例目录；实例归属由调用方负责。
    """

    option_specs: dict[str, dict[str, Any]] = {
        "language": {"type": "string"},
        "fullscreen": {"type": "bool"},
        "vsync": {"type": "bool"},
        "renderDistance": {"type": "int", "min": 2, "max": 32},
        "guiScale": {"type": "int", "min": 0, "max": 4},
        "fov": {"type": "float", "min": 30.0, "max": 110.0},
        "sensitivity": {"type": "float", "min": 0.0, "max": 1.0},
        "mouseSensitivity": {"type": "float", "min": 0.0, "max": 1.0},
        "gamma": {"type": "float", "min": 0.0, "max": 5.0},
        "difficulty": {"type": "int", "min": 0, "max": 3},
        "particles": {"type": "int", "min": 0, "max": 2},
        "clouds": {"type": "int", "min": 0, "max": 3},
    }
    sound_categories = frozenset(
        {"master", "music", "records", "weather", "block", "hostile", "neutral", "player", "ambient", "voice"}
    )

    @classmethod
    def _spec(cls, key: str) -> dict[str, Any] | None:
        if key in cls.option_specs:
            return cls.option_specs[key]
        if key.startswith("soundCategory_") and key.removeprefix("soundCategory_") in cls.sound_categories:
            return {"type": "float", "min": 0.0, "max": 1.0}
        return None

    @staticmethod
    def _load_lines(path: Path) -> list[str]:
        if not path.is_file():
            return []
        return path.read_text(encoding="utf-8", errors="replace").splitlines()

    @staticmethod
    def _coerce(kind: str, value: str) -> Any:
        try:
            if kind == "bool":
                return value.strip().casefold() == "true"
            if kind == "int":
                return int(value)
            if kind == "float":
                return float(value)
            return value
        except ValueError:
            return None

    @classmethod
    def read(cls, options_path: Path) -> dict[str, Any]:
        """
        读取 options.txt，仅返回可结构化编辑的常用键及其取值约束。

        :param options_path: 目标实例的 options.txt 路径
        :return: 包含 ``options`` 列表与 ``ignoredCount`` 的结果
        """
        entries: list[dict[str, Any]] = []
        ignored = 0
        for line in cls._load_lines(options_path):
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            spec = cls._spec(key)
            if spec is None:
                ignored += 1
                continue
            coerced = cls._coerce(spec["type"], value.strip())
            if coerced is None:
                ignored += 1
                continue
            entries.append(
                {
                    "key": key,
                    "value": coerced,
                    "type": spec["type"],
                    "min": spec.get("min"),
                    "max": spec.get("max"),
                }
            )
        return {"path": str(options_path), "options": entries, "ignoredCount": ignored}

    @classmethod
    def patch(cls, options_path: Path, patch: dict[str, Any]) -> dict[str, Any]:
        """
        按受限 schema 写入指定的 options.txt 键，保留未知行与原有顺序。

        :param options_path: 目标实例的 options.txt 路径
        :param patch: ``{键: 值}`` 字典，键必须属于可编辑 schema
        :return: 包含写入实例路径与更新键数的结果
        :raises GameDataError: 键不受支持或值不合法时抛出
        """
        if not isinstance(patch, dict) or not patch:
            raise GameDataError("没有需要修改的游戏选项", "INVALID_GAME_OPTION")
        to_write: dict[str, str] = {}
        for key, value in patch.items():
            spec = cls._spec(str(key))
            if spec is None:
                raise GameDataError("该选项暂不支持修改", "UNSUPPORTED_GAME_OPTION")
            to_write[str(key)] = cls._encode(spec, value)
        lines = cls._load_lines(options_path)
        remaining = dict(to_write)
        replaced = 0
        for index, line in enumerate(lines):
            if ":" in line:
                key = line.partition(":")[0]
                if key in remaining:
                    lines[index] = f"{key}:{remaining.pop(key)}"
                    replaced += 1
        appended = 0
        for key, value in remaining.items():
            lines.append(f"{key}:{value}")
            appended += 1
        atomic_write_text(options_path, "\n".join(lines) + ("\n" if lines else ""))
        return {"path": str(options_path), "updated": replaced + appended}

    @staticmethod
    def _encode(spec: dict[str, Any], value: Any) -> str:
        kind = spec["type"]
        if kind == "bool":
            if isinstance(value, str) and value.strip().casefold() in {"true", "false"}:
                value = value.strip().casefold() == "true"
            if not isinstance(value, bool):
                raise GameDataError("开关值无效", "INVALID_GAME_OPTION")
            return "true" if value else "false"
        if kind in ("int", "float"):
            try:
                converted = int(value) if kind == "int" else float(value)
            except (TypeError, ValueError) as exc:
                raise GameDataError("数值格式无效", "INVALID_GAME_OPTION") from exc
            if isinstance(converted, bool):
                raise GameDataError("数值格式无效", "INVALID_GAME_OPTION")
            minimum = spec.get("min")
            maximum = spec.get("max")
            if minimum is not None and converted < minimum:
                raise GameDataError(f"数值不能小于 {minimum}", "INVALID_GAME_OPTION")
            if maximum is not None and converted > maximum:
                raise GameDataError(f"数值不能大于 {maximum}", "INVALID_GAME_OPTION")
            return str(converted)
        return str(value)


__all__ = ["InstanceOptionsStore"]
