# ============================================================
# EuoraCraft Launcher Core
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher.Core
#
# 文件作用：有界读取实例继承元数据与组件声明，供 Java 选择和模组环境识别使用。
#
# 公开接口：
#   - class InstanceMetadata — 保存已读取的继承元数据与加载器组件。
# ============================================================
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from pydantic import JsonValue, TypeAdapter


@dataclass(frozen=True, slots=True)
class InstanceMetadata:
    """
    只读解析已存在的实例声明，提供继承文档与实际加载器组件。

    单个文件无法读取或继承链无效时停止遍历，保留已读取的声明。
    本模块不判定实例是否允许启动，不下载或写入文件。
    """

    documents: tuple[dict[str, JsonValue], ...]
    components: tuple[tuple[str, str], ...]

    json_adapter: ClassVar[TypeAdapter[JsonValue]] = TypeAdapter(JsonValue)
    component_prefixes: ClassVar[tuple[tuple[str, str], ...]] = (
        ("net.minecraftforge:forge:", "Forge"),
        ("net.neoforged:neoforge:", "NeoForge"),
        ("net.neoforged:forge:", "NeoForge"),
        ("net.fabricmc:fabric-loader:", "Fabric"),
        ("org.quiltmc:quilt-loader:", "Quilt"),
        ("optifine:OptiFine:", "OptiFine"),
    )

    @classmethod
    def read(cls, minecraft_root_path: Path, version_id: str) -> InstanceMetadata:
        """
        在版本根目录边界内遍历继承链，最多读取 64 个有界 JSON。

        :param minecraft_root_path: 已解析的 Minecraft 根目录
        :param version_id: 实例目录名
        :return: 已成功读取的元数据；无法读取的文件不会影响其他实例
        """
        documents: list[dict[str, JsonValue]] = []
        visited: set[str] = set()
        current = version_id
        while current:
            if current in visited or len(visited) >= 64:
                break
            if current in {".", ".."} or any(character in current for character in "/\\:"):
                break
            visited.add(current)
            json_path = minecraft_root_path / "versions" / current / f"{current}.json"
            document = cls._read_document(json_path)
            if document is None:
                break
            documents.append(document)
            parent = document.get("inheritsFrom")
            if parent is not None and (not isinstance(parent, str) or not parent.strip()):
                break
            current = parent or ""
        return cls(tuple(documents), cls._components(documents))

    @classmethod
    def _read_document(cls, json_path: Path) -> dict[str, JsonValue] | None:
        """
        限制元数据大小并在不可信 JSON 边界校验对象结构。
        """
        try:
            if json_path.stat().st_size > 8 * 1024 * 1024:
                return None
            document = cls.json_adapter.validate_python(json.loads(json_path.read_text(encoding="utf-8-sig")))
            return document if isinstance(document, dict) and isinstance(document.get("id"), str) else None
        except (OSError, ValueError):
            return None

    @classmethod
    def _components(cls, documents: list[dict[str, JsonValue]]) -> tuple[tuple[str, str], ...]:
        components: dict[str, str] = {}
        for document in reversed(documents):
            libraries = document.get("libraries", [])
            if not isinstance(libraries, list):
                continue
            for library in libraries:
                name = library.get("name") if isinstance(library, dict) else None
                if not isinstance(name, str):
                    continue
                for prefix, component in cls.component_prefixes:
                    if name.casefold().startswith(prefix.casefold()):
                        components[component] = name[len(prefix) :].split(":")[0]
        return tuple(components.items())

    def mod_environment(self, fallback_minecraft_version: str | None = None) -> dict[str, str | None]:
        """
        提供实际声明的游戏与加载器版本，Java 未探测时保留未知。

        :param fallback_minecraft_version: Core 已识别的实际原版版本
        :return: 模组诊断使用的运行环境；不使用 Java 最低要求冒充实际版本
        """
        environment: dict[str, str | None] = {"minecraft": fallback_minecraft_version, "java": None}
        if not environment["minecraft"] and self.documents:
            identifier = self.documents[-1].get("id")
            environment["minecraft"] = (
                identifier
                if isinstance(identifier, str)
                and re.fullmatch(r"\d+(?:\.\d+)+(?:[-_].+)?|\d{2}w\d{2}[a-z]", identifier)
                else None
            )
        for name, version in self.components:
            if name == "Fabric":
                environment["fabricloader"] = version
            elif name == "Quilt":
                environment["quilt_loader"] = version
            elif name == "Forge":
                environment["forge"] = version.split("-", 1)[-1]
            elif name == "NeoForge":
                environment["neoforge"] = version
        return environment
