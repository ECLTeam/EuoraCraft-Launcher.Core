from .NetLibs import BaseApiClient
from pathlib import Path
from . import Libs
import json


class FilesChecker:
    def __init__(
        self,
        api_client: BaseApiClient
    ):
        """
        实例化一个文件检查器, 用于检查 Minecraft 文件的完整型
        :param api_client: BaseApiClient 实例
        """
        self.api_client = api_client
        self.config = self.api_client.config

    def _resolve(self, url: str, path: str) -> str:
        """
        返回最匹配的仓库基础 URL
        :param url: URL
        :param path: Path
        :return: API URL
        """
        combined = (url + path).lower()

        if "fabric" in combined:
            return self.config.Fabric
        elif "neoforge" in combined:
            return self.config.NeoForged
        elif "forge" in combined:
            return self.config.Forge
        elif "quilt" in combined:
            return self.config.Quilt
        # 默认回退到官方 libraries 仓库
        return self.config.Libraries

    def check_game_jar(self, game_path: Path, version_name: str, version_json: dict, jar_name: str | None = None) -> list[tuple[str, str]]:  # 检查游戏本体
        download_list = []
        if "client" in version_json.get("downloads", {}):
            jar_name = jar_name or version_name
            game_jar_path = game_path / "versions" / version_name / f"{jar_name}.jar"
            jar_sha1 = version_json["downloads"]["client"]["sha1"]

            if Libs.get_file_sha1(game_jar_path) != jar_sha1:
                download_list.append((self.api_client.get_client_jar_url(jar_sha1), game_jar_path))

        return download_list

    def check_libraries(self, game_path: Path, version_json: dict) -> list[tuple[str, str]]:
        download_list = []
        for libraries in version_json.get("libraries", []):
            if "classifiers" in libraries.get("downloads", {}):  # 补全natives
                for classifiers in libraries["downloads"]["classifiers"].values():
                    natives_path = game_path / "libraries" / classifiers["path"]

                    if Libs.get_file_sha1(natives_path) == classifiers["sha1"]:
                        continue

                    download_list.append(
                        (
                            f"{self._resolve(classifiers['url'], classifiers['path'])}/{classifiers['path']}",
                            str(natives_path)
                        )
                    )

            lib_path = Libs.name_to_path(libraries["name"])

            if not lib_path: continue

            libraries_path = game_path / "libraries" / lib_path
            file_sha1 = ""

            if "sha1" in libraries:
                file_sha1 = libraries["sha1"]
            elif "sha1" in libraries.get("downloads", {}).get("artifact", {}):
                file_sha1 = libraries["downloads"]["artifact"]["sha1"]
            if (not file_sha1 and libraries_path.is_file()) or Libs.get_file_sha1(libraries_path) == file_sha1:
                continue

            raw_url = ""

            if "url" in libraries:
                raw_url = libraries["url"]
            elif libraries.get("downloads", {}).get("artifact", {}).get("url"):
                raw_url = libraries["downloads"]["artifact"]["url"]

            download_list.append(
                (
                    f"{self._resolve(raw_url, lib_path)}/{lib_path}",
                    str(libraries_path)
                )
            )

        return download_list


    def check_assets(self, game_path: Path, version_json: dict) -> list[tuple[str, str]]:
        download_list = []
        asset_index = version_json.get("assetIndex", {})

        if not asset_index:
            return download_list

        asset_id = asset_index["id"]
        file_sha1 = asset_index["sha1"]
        local_index_path = game_path / "assets" / "indexes" / f"{asset_id}.json"

        # 如果本地索引不存在或 SHA1 不匹配，重新下载
        if Libs.get_file_sha1(local_index_path) != file_sha1:
            # 资源索引请求失败必须传播给上层，否则下载任务会误报完成。
            index_data = self.api_client.get_asset_index(asset_id, file_sha1)
            local_index_path.parent.mkdir(parents=True, exist_ok=True)
            local_index_path.write_text(json.dumps(index_data), encoding="utf-8")
        else:
            index_data = json.loads(local_index_path.read_text("utf-8"))

        base_assets = self.config.Assets

        for assets in index_data["objects"].values():
            asset_file_sha1 = assets["hash"]
            get_asset_path = f"{asset_file_sha1[:2]}/{asset_file_sha1}"
            asset_path = game_path / "assets" / "objects" / get_asset_path
            if Libs.get_file_sha1(asset_path) == asset_file_sha1: continue
            download_list.append((f"{base_assets}/{get_asset_path}", str(asset_path)))

        return download_list

    def check_files(self, game_path: str | Path, version_name: str) -> list[tuple[str, str]]:
        """
        检查 game_path 路径的 version_name 版本的完整性
        :param game_path: .minecraft 路径
        :param version_name: 版本名称
        :return: 需要下载的列表 [("URL", "Path")]
        """
        game_path = Path(game_path)
        download_list = []

        if not (game_path / "versions" / version_name / f"{version_name}.json").is_file():
            return download_list

        version_json = json.loads((game_path / "versions" / version_name / f"{version_name}.json").read_text("utf-8"))
        download_list.extend(self.check_game_jar(game_path, version_name, version_json))
        download_list.extend(self.check_libraries(game_path, version_json))
        download_list.extend(self.check_assets(game_path, version_json))
        game_json = Libs.find_version(version_json, game_path, version_name)

        if game_json:
            jar_name = game_json[1].name
            if "inheritsFrom" in version_json:
                json_path = game_path / "versions" / version_name / f"{version_json['inheritsFrom']}.json"
                if json_path.is_file():
                    jar_name = json_path.stem
            download_list.extend(self.check_game_jar(game_path, game_json[1].name, game_json[0], jar_name=jar_name))
            download_list.extend(self.check_libraries(game_path, game_json[0]))
            download_list.extend(self.check_assets(game_path, game_json[0]))

        return download_list

