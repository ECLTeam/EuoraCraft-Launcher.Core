from .LoaderInstaller import LoaderInstaller
from .FilesChecker import FilesChecker
from shutil import rmtree
from pathlib import Path
import json


class VersionClassifier:
    """独立的版本分类器，可单独测试和扩展"""
    @staticmethod
    def classify(versions: dict) -> dict:
        """
        Minecraft 版本分类
        :param versions: 版本清单列表
        :return: {"分类": [版本列表], "映射": {...}}
        """
        special_fool_days = ["1.RV-Pre1"]
        all_ver, release, snapshot, fool_days, beta, alpha = [], [], [], [], [], []
        mapping = {}
        for v in versions:
            if "release" in v["type"]:
                v["type"] = "Release"
                release.append(v)
            elif "snapshot" in v["type"]:
                if "-04-01" in v["releaseTime"] or v["id"] in special_fool_days:
                    v["type"] = "FoolDay"
                    fool_days.append(v)
                else:
                    v["type"] = "Snapshot"
                    snapshot.append(v)
            elif "beta" in v["type"]:
                v["type"] = "Beta"
                beta.append(v)
            elif "alpha" in v["type"]:
                v["type"] = "Alpha"
                alpha.append(v)
            all_ver.append(v)
            mapping.update({v["id"]: v})

        return {
            "All": all_ver,  # 所有版本 [{"...": "..."}]
            "Release": release,    # 正式版 [{"...": "..."}]
            "Snapshot": snapshot,  # 快照版 [{"...": "..."}]
            "FoolDays": fool_days,  # 愚人节版 [{"...": "..."}]
            "Beta": beta,  # Beta版 [{"...": "..."}]
            "Alpha": alpha,  # Alpha版 [{"...": "..."}]
            "Mapping": mapping  # 映射表 {"<版本ID>": {...}}
        }


class GetGames:
    def __init__(
        self,
        files_checker: FilesChecker,
        loader_installer: LoaderInstaller,
        game_path: Path | str
    ):
        """
        获取游戏基类
        :param files_checker: FilesChecker 实例
        :param game_path: .minecraft 路径
        """
        self.files_checker = files_checker
        self.config = files_checker.config
        self.api_client = files_checker.api_client
        self.loader_installer = loader_installer
        self.game_path = Path(game_path)

    def _save_version_info(self, version_name: str, version_info: dict) -> None:
        json_path = self.game_path / "versions" / version_name / "VersionInfo.json"
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps(version_info, ensure_ascii=False, indent=4), encoding="utf-8")

    def get_minecraft_versions(self) -> dict:
        """
        获取版本清单列表并分类和映射
        :return: {"Latest": {上一个版本},"分类": [版本列表], "映射": {...}}
        """
        manifest = self.api_client.get_minecraft_manifest()
        return {
            "Latest": manifest["latest"],  # 上一个版本 {"release": "...", "snapshot": "..."}
            **VersionClassifier.classify(manifest["versions"])
        }

    def build_minecraft_download_list(
        self,
        version_id: str,
        save_name: str | None = None,
        save_version_info: bool = True
    ) -> list[tuple[str, str]] | tuple[str, dict]:
        """
        下载指定版本的 Minecraft
        :param version_id: 版本 ID
        :param save_name: 保存名称
        :param save_version_info: 是否保存版本信息缓存
        :return: 若 save_version_info 为 True 则返回下载列表, False 则只返回版本类型
        """
        save_name = save_name or version_id
        # 1. 获取版本 JSON（客户端封装了重试）
        manifest = self.get_minecraft_versions()["Mapping"]
        if version_id not in manifest:
            raise KeyError(f"未找到 Minecraft 版本 '{version_id}'")
        manifest = manifest[version_id]
        version_data = self.api_client.get_minecraft_json(version_id, manifest["sha1"])

        # 2. 保存 JSON
        json_path = self.game_path / "versions" / save_name / (f"{save_name}.json" if save_version_info else f"{version_id}.json")
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps(version_data, ensure_ascii=False, indent=4), encoding="utf-8")

        # 3. 更新元数据
        if save_version_info:
            self._save_version_info(save_name, {
                "VanillaType": manifest["type"],
                "VanillaVersion": version_id,
            })
            return self.files_checker.check_files(self.game_path, save_name)
        return manifest["type"], version_data

    def get_fabric_versions(self, game_version_id: str) -> dict[str, list[dict]] | None:
        """
        获取指定某个 Minecraft 版本可用的 Fabric 版本列表
        :param game_version_id: 版本 ID
        :return: Fabric 版本列表
        """
        fabric_versions = self.api_client.get_fabric_versions(game_version_id)
        if not fabric_versions:
            return None
        all_versions = []
        stable_versions = []
        not_stable_versions = []
        for fabric_version in fabric_versions:
            is_stable = fabric_version["loader"]["stable"]
            version_info = {
                "LoaderVersion": fabric_version["loader"]["version"],
                "GameVersion": game_version_id,
                "Stable": is_stable
            }
            all_versions.append(version_info)
            if is_stable:
                stable_versions.append(version_info)
            else:
                not_stable_versions.append(version_info)
        return {
            "All": all_versions,
            "Stable": stable_versions,
            "NotStable": not_stable_versions
        }


    def build_fabric_download_list(
        self,
        game_version_id: str,
        loader_version: str,
        save_name: str | None = None,
        fabric_api: tuple[str, str] | None = None
    ) -> list[tuple[str, str]]:
        """
        下载指定 Minecraft 版本的指定 Fabric
        :param game_version_id: 版本 ID
        :param loader_version: Loader 版本，为空时解析最新版本
        :param save_name: 保存名称
        :param fabric_api: 可选 Fabric API 下载信息 (URL, 文件名)
        :return: 下载列表 [("URL", "PATH")]
        """
        save_name = save_name or f"{game_version_id}-Fabric"

        mc_type = self.build_minecraft_download_list(
            version_id=game_version_id,
            save_name=save_name,
            save_version_info=False
        )[0]
        if not loader_version:
            fabric_versions = self.api_client.get_fabric_versions(game_version_id)
            if not fabric_versions:
                raise ValueError(f"未找到 {game_version_id} 的 Fabric 版本")
            loader_version = fabric_versions[0]["loader"]["version"]
        version_data = self.api_client.get_fabric_profile(
            game_version_id=game_version_id,
            loader_version=loader_version
        )

        json_path = self.game_path / "versions" / save_name / f"{save_name}.json"
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps(version_data, ensure_ascii=False, indent=4), encoding="utf-8")

        self._save_version_info(save_name, {
            "VanillaType": mc_type,
            "VanillaVersion": game_version_id,
            "LoaderType": "Fabric",
            "LoaderVersion": loader_version
        })

        download_list = self.files_checker.check_files(self.game_path, save_name)
        if fabric_api:
            url, filename = fabric_api
            download_list.append((url, str(self.game_path / "mods" / filename)))
        return download_list

    def get_neoforged_versions(self, game_version_id: str) -> dict[str, list]:
        """
        获取指定某个 Minecraft 版本可用的 NeoForged 版本列表
        :param game_version_id: 版本 ID
        :return: NeoForged 版本列表
        """
        return self.api_client.get_neoforged_versions(game_version_id)


    def build_neoforged_download_list(
        self,
        game_version_id: str,
        loader_version: str,
        java_path: Path | str,
        save_name: str | None = None
    ) -> list[tuple[str, str]]:
        """
        下载指定 Minecraft 版本的指定 NeoForged
        :param game_version_id: 版本 ID
        :param loader_version: Loader 版本
        :param java_path: Java Path
        :param save_name: 保存名称
        :return: 下载列表 [("URL", "PATH")]
        """
        save_name = save_name or f"{game_version_id}-NeoForged"

        mc_type, game_version_data = self.build_minecraft_download_list(
            version_id=game_version_id,
            save_name=save_name,
            save_version_info=False
        )
        jar_path = self.game_path / "versions" / save_name / f"{game_version_id}.jar"
        self.api_client.download_client_jar(game_version_data["downloads"]["client"]["sha1"], jar_path)

        save_path = self.game_path / "versions" / save_name / "InstallCache"
        save_path.mkdir(parents=True, exist_ok=True)
        installer_path = self.api_client.download_neoforged_installer(game_version_id, loader_version, save_path)
        self.loader_installer.install_neoforged(installer_path, java_path, save_name)
        rmtree(save_path)

        self._save_version_info(save_name, {
            "VanillaType": mc_type,
            "VanillaVersion": game_version_id,
            "LoaderType": "NeoForged",
            "LoaderVersion": loader_version
        })

        return self.files_checker.check_files(self.game_path, save_name)

    def get_forge_versions(self, game_version_id: str) -> list[dict[str, str]]:
        """
        获取指定某个 Minecraft 版本可用的 Forge 版本列表
        :param game_version_id: 版本 ID
        :return: Forge 版本列表
        """
        return self.api_client.get_forge_versions(game_version_id)

    def build_forge_download_list(
        self,
        game_version_id: str,
        loader_version: str,
        java_path: Path | str,
        save_name: str | None = None
    ) -> list[tuple[str, str]]:
        """
        下载指定 Minecraft 版本的指定 Forge
        :param game_version_id: 版本 ID
        :param loader_version: Loader 版本
        :param java_path: Java Path
        :param save_name: 保存名称
        :return: 下载列表 [("URL", "PATH")]
        """
        save_name = save_name or f"{game_version_id}-Forge"

        #"""
        mc_type, game_version_data = self.build_minecraft_download_list(
            version_id=game_version_id,
            save_name=save_name,
            save_version_info=False
        )
        jar_path = self.game_path / "versions" / save_name / f"{game_version_id}.jar"
        self.api_client.download_client_jar(game_version_data["downloads"]["client"]["sha1"], jar_path)
        #"""
        #mc_type = "Release"

        save_path = self.game_path / "versions" / save_name / "InstallCache"
        save_path.mkdir(parents=True, exist_ok=True)
        installer_path = self.api_client.download_forge_installer(game_version_id, loader_version, save_path)
        #installer_path = r""
        self.loader_installer.install_neoforged(installer_path, java_path, save_name)
        rmtree(save_path)

        self._save_version_info(save_name, {
            "VanillaType": mc_type,
            "VanillaVersion": game_version_id,
            "LoaderType": "Forge",
            "LoaderVersion": loader_version
        })

        return self.files_checker.check_files(self.game_path, save_name)

    def get_quilt_versions(self, game_version_id: str) -> dict[str, list] | None:
        """
        获取指定某个 Minecraft 版本可用的 Quilt 版本列表
        :param game_version_id: 版本 ID
        :return: Quilt 版本列表
        """
        quilt_support = self.api_client.get_quilt_support()
        support_games = [ver["version"] for ver in quilt_support]
        if game_version_id not in support_games:
            return None
        quilt_versions = self.api_client.get_quilt_versions()
        all_versions = []
        stable_versions = []
        beta_versions = []
        for quilt_version in quilt_versions:
            version_name = quilt_version["version"]
            version_info = {
                "LoaderVersion": version_name,
                "LoaderType": "Stable"
            }
            if "beta" in version_name.lower():
                version_info["LoaderType"] = "Beta"
                beta_versions.append(version_info)
            all_versions.append(version_info)
        return {
            "All": all_versions,
            "Stable": stable_versions,
            "Beta": beta_versions
        }

    def build_quilt_download_list(
        self,
        game_version_id: str,
        loader_version: str,
        save_name: str | None = None
    ) -> list[tuple[str, str]]:
        """
        下载指定 Minecraft 版本的指定 Quilt
        :param game_version_id: 版本 ID
        :param loader_version: Loader 版本
        :param save_name: 保存名称
        :return: 下载列表 [("URL", "PATH")]
        """
        save_name = save_name or f"{game_version_id}-Quilt"

        mc_type = self.build_minecraft_download_list(
            version_id=game_version_id,
            save_name=save_name,
            save_version_info=False
        )[0]
        version_data = self.api_client.get_quilt_profile(
            game_version_id=game_version_id,
            loader_version=loader_version
        )

        json_path = self.game_path / "versions" / save_name / f"{save_name}.json"
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps(version_data, ensure_ascii=False, indent=4), encoding="utf-8")

        self._save_version_info(save_name, {
            "VanillaType": mc_type,
            "VanillaVersion": game_version_id,
            "LoaderType": "Quilt",
            "LoaderVersion": loader_version
        })

        return self.files_checker.check_files(self.game_path, save_name)

