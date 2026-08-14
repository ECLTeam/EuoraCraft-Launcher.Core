from pathlib import Path
from ..Core import Libs
import zipfile
import json
import re


class SearchMinecraft:
    def __init__(self, game_path: Path | str):
        """
        初始化
        :param game_path: .minecraft 路径
        """
        self.game_path = Path(game_path)

    @staticmethod
    def _find_ver_from_class(_class_stream):
        constant_utf8 = 1
        constant_integer = 3
        constant_float = 4
        constant_long = 5
        constant_double = 6
        constant_class = 7
        constant_string = 8
        constant_fieldref = 9
        constant_methodref = 10
        constant_interfacemethodref = 11
        constant_nameandtype = 12
        constant_methodhandle = 15
        constant_methodtype = 16
        constant_dynamic = 17
        constant_invokedynamic = 18
        constant_module = 19
        constant_package = 20

        # 标签对应的固定数据长度（字节数）
        tag_fixed_size = {
            constant_integer: 4,
            constant_float: 4,
            constant_long: 8,
            constant_double: 8,
            constant_class: 2,
            constant_string: 2,
            constant_methodtype: 2,
            constant_module: 2,
            constant_package: 2,
            constant_fieldref: 4,
            constant_methodref: 4,
            constant_interfacemethodref: 4,
            constant_nameandtype: 4,
            constant_dynamic: 4,
            constant_invokedynamic: 4,
            constant_methodhandle: 3,
        }

        def read_u2(stream):
            return int.from_bytes(stream.read(2), "big")

        def read_u4(stream):
            return int.from_bytes(stream.read(4), "big")

        def read_constant_pool_utf8_strings(class_stream):
            strings = []

            # 检查魔数
            if read_u4(class_stream) != 0xCAFEBABE:
                raise ValueError("Not a valid Java class file")

            # 跳过 minor_version, major_version
            class_stream.read(4)

            pool_count = read_u2(class_stream)
            i = 1
            while i < pool_count:
                tag = class_stream.read(1)[0]

                if tag == constant_utf8:
                    length = read_u2(class_stream)
                    data = class_stream.read(length)
                    strings.append(data.decode("utf-8"))
                else:
                    size = tag_fixed_size.get(tag)
                    if size is None:
                        raise ValueError(f"Unknown constant pool tag: {tag}")
                    class_stream.read(size)
                    # Long 和 Double 占用两个索引
                    if tag in (constant_long, constant_double):
                        i += 1

                i += 1

            return strings
        return read_constant_pool_utf8_strings(_class_stream)

    @staticmethod
    def _find_ver_from_server_class(_class_stream):
        pass

    def _find_game_ver_from_jar(self, jar_path: Path | str) -> str | None:
        with zipfile.ZipFile(jar_path, "r") as zf:
            try:
                ver_json = json.loads(zf.read("version.json"))
                if "id" in ver_json:
                    return ver_json["id"]
            except (KeyError, json.decoder.JSONDecodeError):
                pass
            try:
                with zf.open("net/minecraft/client/Minecraft.class", "r") as stream:
                    utf8_strings = self._find_ver_from_class(stream)
                    version = ""
                    for s in utf8_strings:
                        if s.startswith("Minecraft Minecraft "):
                            version = s[len("Minecraft Minecraft "):]
                            break
                    if version:
                        # 过滤 RC/Beta/Alpha 的内部标记
                        if version in ("RC1", "RC2"):
                            return None
                        if version.startswith("Beta "):
                            return "b" + version[len("Beta "):]
                        if version.startswith("Alpha v"):
                            return "a" + version[len("Alpha v"):]
                        return version
                    return "Unknown"
            except (KeyError, ValueError):
                pass
            try:
                with zf.open("net/minecraft/server/MinecraftServer.class", "r") as stream:
                    utf8_strings = self._find_ver_from_class(stream)
                    # 找到 "Can't keep up!" 的索引
                    can_keep_up_idx = -1
                    for i, s in enumerate(utf8_strings):
                        if s.startswith("Can't keep up!"):
                            can_keep_up_idx = i
                            break
                    if can_keep_up_idx >= 0:
                        # 向前搜索包含数字的字符串
                        version_pattern = re.compile(r'^.*\d.*$')
                        for i in range(can_keep_up_idx - 1, -1, -1):
                            if version_pattern.match(utf8_strings[i]):
                                return utf8_strings[i]
            except (KeyError, ValueError):
                pass
            return None


    def _find_minecraft_req_java(self, version_json: dict, version_name: str | None = None) -> str:
        req_java = version_json.get("javaVersion", {}).get("majorVersion")
        if req_java:
            return str(req_java)
        game_json = Libs.find_version(version_json, self.game_path, version_name)
        if game_json:
            return str(game_json[0].get("javaVersion", {}).get("majorVersion", "Unknown"))
        return "Unknown"

    def _find_minecraft_type(self, version_json: dict, version_name: str | None = None, version_id: str = "Unknown") -> str:
        def _check_minecraft_type(mc_type: str, release_time: str):
            special_fool_days = ["1.RV-Pre1"]

            if "release" in mc_type:
                return "Release"
            elif "snapshot" in mc_type:
                if "-04-01" in release_time or version_id in special_fool_days:
                    return "FoolDay"
                else:
                    return "Snapshot"
            elif "beta" in mc_type:
                return "Beta"
            elif "alpha" in mc_type:
                return "Alpha"
            return "Unknown"

        game_json = Libs.find_version(version_json, self.game_path, version_name)
        if game_json:
            return _check_minecraft_type(game_json[0]["type"], game_json[0]["releaseTime"])
        if "type" in version_json and "releaseTime" in version_json:
            return _check_minecraft_type(version_json["type"], version_json["releaseTime"])
        return "Unknown"

    @staticmethod
    def _find_loader_type(version_json: dict) -> str | None:
        ver_libs = json.dumps(version_json["libraries"], ensure_ascii=False).lower()
        if "neoforge" in ver_libs:
            return "NeoForge"
        elif "forge" in ver_libs:
            return "Forge"
        elif "quilt" in ver_libs:
            return "Quilt"
        elif "legacyfabric" in ver_libs:
            return "LegacyFabric"
        elif "babric" in ver_libs:
            return "Babric"
        elif "fabric" in ver_libs:
            return "Fabric"
        elif "liteloader" in ver_libs:
            return "LiteLoader"
        elif "cleanroom" in ver_libs:
            return "Cleanroom"
        elif "optifine" in ver_libs:
            return "OptiFine"
        return None

    @staticmethod
    def _find_loader_version(version_json: dict, loader_type: str) -> str:
        if loader_type in {"NeoForge", "NeoForged"}:
            args_iter = iter(list(version_json["arguments"]["game"]))
            for arg in args_iter:
                if arg == "--fml.neoForgeVersion":
                    return next(args_iter)
        elif loader_type == "Forge":
            for lib in version_json["libraries"]:
                split_name = lib["name"].split(":")
                if split_name[1].lower() == "forge":
                    if "-" in split_name[2]:
                        split_ver = split_name[2].split("-")
                        if len(split_ver) >= 2:
                            return split_ver[1]
                    return split_name[2]
                elif split_name[1].lower() == "fmlloader":
                    if "-" in split_name[2]:
                        split_ver = split_name[2].split("-")
                        if len(split_ver) >= 2:
                            return split_ver[1]
                    return split_name[2]
        elif loader_type in ("Fabric", "LegacyFabric"):
            for lib in version_json["libraries"]:
                split_name = lib["name"].split(":")
                if split_name[1].lower() in ("fabric", "fabric-loader"):
                    return split_name[2]
        elif loader_type == "Quilt":
            for lib in version_json["libraries"]:
                split_name = lib["name"].split(":")
                if split_name[1].lower() in ("quilt", "quilt-loader"):
                    return split_name[2]
        elif loader_type == "OptiFine":
            for lib in version_json["libraries"]:
                split_name = lib["name"].split(":")
                if split_name[1].lower() == "optifine":
                    if "-" in split_name[2]:
                        split_ver = split_name[2].split("-")
                        if len(split_ver) >= 2:
                            return split_ver[1]
                    return split_name[2]
        elif loader_type == "LiteLoader":
            for lib in version_json["libraries"]:
                split_name = lib["name"].split(":")
                if split_name[1].lower() == "liteloader":
                    return split_name[2]
        elif loader_type == "Cleanroom":
            for lib in version_json["libraries"]:
                split_name = lib["name"].split(":")
                if split_name[1].lower() == "cleanroom":
                    return split_name[2]
        return "Unknown"

    def search_minecraft(self):
        """搜索路径中的所有 Minecraft 版本"""
        versions = {}
        versions_path = self.game_path / "versions"
        for version_dir in versions_path.iterdir():
            try:
                if not version_dir.is_dir():
                    continue
                ver_info = version_dir / "VersionInfo.json"
                if ver_info.is_file():
                    try:
                        info_json = json.loads(ver_info.read_text("utf-8"))
                        if "VanillaVersion" in info_json:
                            info_json["VersionPath"] = str(version_dir)
                            version_json = json.loads((version_dir / f"{version_dir.name}.json").read_text("utf-8"))
                            info_json["RequestJava"] = self._find_minecraft_req_java(version_json, version_dir.name)
                            versions[version_dir.name] = info_json
                            continue
                    except json.decoder.JSONDecodeError:
                        pass

                info = {
                    "VanillaType": "Unknown",
                    "VanillaVersion": "Unknown",
                    "VersionPath": str(version_dir),
                    "RequestJava": "Unknown"
                }
                ver_json = version_dir / f"{version_dir.name}.json"
                if not ver_json.is_file():
                    continue
                version_json: dict = json.loads(ver_json.read_text("utf-8"))
                if "inheritsFrom" in version_json:
                    game_jar = version_dir / f"{version_json["inheritsFrom"]}.jar"
                    if game_jar.is_file():
                        game_ver = self._find_game_ver_from_jar(game_jar)
                        if game_ver:
                            info["VanillaVersion"] = game_ver

                game_json = Libs.find_version(version_json, self.game_path, version_dir.name)
                if game_json:
                    game_jar= game_json[1] / f"{game_json[1].name}.jar"
                    if game_jar.is_file():
                        game_ver = self._find_game_ver_from_jar(game_jar)
                        if game_ver:
                            info["VanillaVersion"] = game_ver
                else:
                    ver_jar = version_dir / f"{version_dir.name}.jar"
                    if ver_jar.is_file():
                        game_ver = self._find_game_ver_from_jar(ver_jar)
                        if game_ver:
                            info["VanillaVersion"] = game_ver

                if info["VanillaVersion"] == "Unknown":
                    game_args: list | None = version_json.get("arguments", {}).get("game")
                    if game_args:
                        args_iter = iter(game_args)
                        for arg in args_iter:
                            if arg == "--fml.mcVersion":
                                info["VanillaVersion"] = next(args_iter)

                loader_type = self._find_loader_type(version_json)
                if loader_type:
                    info["LoaderType"] = loader_type
                    info["LoaderVersion"] = self._find_loader_version(version_json, loader_type)
                info["VanillaType"] = self._find_minecraft_type(version_json, version_dir.name, info["VanillaVersion"])
                info["RequestJava"] = self._find_minecraft_req_java(version_json, version_dir.name)

                versions[version_dir.name] = info
            except:
                pass
        # print(json.dumps(versions, ensure_ascii=False, indent=4))
        return versions
