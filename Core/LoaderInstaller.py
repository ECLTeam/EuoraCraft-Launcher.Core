from .InstancesManager import InstancesManager
from .FilesChecker import FilesChecker
from .Downloader import Downloader
from typing import Callable
from pathlib import Path
from . import Libs
import platform
import zipfile
import asyncio
import json
import re


class LoaderInstaller:
    def __init__(
        self,
        files_checker: FilesChecker,
        instances_mgr: InstancesManager,
        game_path: Path | str,
        log_callback: Callable[[str], None] | None = None
     ):
        """
        初始化
        :param files_checker: FilesChecker 实例
        :param instances_mgr: InstancesManager 实例
        :param game_path: .minecraft 路径
        :param log_callback: Log 回调
        """
        self.files_checker = files_checker
        self.config = files_checker.config
        self.instances_mgr = instances_mgr
        self.game_path = Path(game_path)
        self.log_callback = log_callback or (lambda log: print(log))

        self.cp_delimiter = ":" if platform.system() != "Windows" else ";"

    def install_neoforged(
        self,
        installer_path: Path | str,
        java_path: Path | str,
        save_name: str
    ) -> bool:
        """
        安装 NeoForged 或高版本 Forge(会回退旧版安装)
        :param installer_path: Installer.jar 路径
        :param java_path: Java Path
        :param save_name: 保存名称
        :return: bool
        """
        installer_path = Path(installer_path)
        try:
            with zipfile.ZipFile(installer_path, "r") as zf:
                install_profile = json.loads(zf.read("install_profile.json"))
                version_data = json.loads(zf.read("version.json"))
                bin_name = install_profile["data"]["BINPATCH"]["client"].replace("[", "").replace("]", "").replace("'", "").replace("/", "", 1)
                patch_bin = zf.read(bin_name)
        except:
            return self.install_forge(installer_path, save_name)
        if "processors" not in install_profile or not install_profile["processors"]:
            return self.install_forge(installer_path, save_name)
        java_path = Path(java_path)
        ver_path = self.game_path / "versions" / save_name
        (ver_path / f"{save_name}.json").write_text(json.dumps(version_data, ensure_ascii=False, indent=4), encoding="utf-8")
        bin_path = installer_path.parent / "client.bin"
        bin_path.write_bytes(patch_bin)

        download_list = self.files_checker.check_libraries(self.game_path, install_profile)
        download_list.extend(self.files_checker.check_libraries(self.game_path, version_data))
        if download_list:
            downloader = Downloader(
                download_list,
                skip_preflight=True,
                progress_callback=lambda done, total: self.log_callback(f"[Downloader] 进度: {done}/{total} ({done / total:.2%})"),
                speed_callback=lambda sp: self.log_callback(f"[Downloader] 速度: {sp:.2f} MB/s")
            )
            asyncio.run(downloader.run())

        patched_name = install_profile["data"]["PATCHED"]["client"].replace("[", "").replace("]", "").replace("'", "")
        patched_path = self.game_path / "libraries" / f"{Libs.name_to_path(patched_name)}"
        patched_path.parent.mkdir(parents=True, exist_ok=True)

        processors = []
        for processor in install_profile["processors"]:
            if "sides" in processor and "client" not in processor["sides"]:
                continue

            args = []
            for arg in processor["args"]:
                if "{ROOT}" in arg:
                    arg = f'"{arg.replace("{ROOT}", str(self.game_path))}"'
                elif "{LIBRARY_DIR}" in arg:
                    arg = f'"{arg.replace("{LIBRARY_DIR}", str(self.game_path / "libraries"))}"'
                args.append(arg.replace(" ", ""))
            jvm_args = (
                " ".join(args)
                .replace("{MINECRAFT_JAR}", f'"{ver_path / version_data.get("inheritsFrom", "None")}.jar"')
                .replace("{PATCHED}", f'"{patched_path}"')
                .replace("{BINPATCH}", f'"{bin_path}"')
                .replace("{MINECRAFT_VERSION}", f'"{version_data.get("inheritsFrom")}"')
                .replace("{INSTALLER}", f'"{installer_path}"')
                .replace("{SIDE}", "client")
            )

            classpath = []
            for cp_name in processor["classpath"]:
                lib_path = self.game_path / "libraries" / f"{Libs.name_to_path(cp_name)}"
                classpath.append(str(lib_path))

            if processor["jar"] not in processor["classpath"]:
                lib_path = self.game_path / "libraries" / f"{Libs.name_to_path(processor["jar"])}"
                classpath.append(str(lib_path))
            cp = self.cp_delimiter.join(classpath)

            jar_path = self.game_path / "libraries" / f"{Libs.name_to_path(processor['jar'])}"
            with zipfile.ZipFile(jar_path, "r") as zf:
                manifest_mf = zf.read("META-INF/MANIFEST.MF").decode("utf-8")
            match = re.search(r"^Main-Class:\s*(.+)$", manifest_mf, re.MULTILINE)
            if match:
                main_class = match.group(1).strip()
                processors.append(f'"{java_path}" -cp "{cp}" "{main_class}" {jvm_args}')

        for args in processors:
            self.instances_mgr.create_instance(
                instance_name="NeoForged Installer",
                instance_type="LoaderInstaller",
                args=args,
                cwd=installer_path.parent,
                block_thread=True
            )
        return True

    def install_forge(
        self,
        installer_path: Path | str,
        save_name: str
    ) -> bool:
        """
        安装旧版 Forge
        :param installer_path: Installer.jar 路径
        :param save_name: 保存名称
        :return: bool
        """
        installer_path = Path(installer_path)
        with zipfile.ZipFile(installer_path, "r") as zf:
            install_profile = json.loads(zf.read("install_profile.json"))
            try:
                version_data = json.loads(zf.read("version.json"))
            except KeyError:
                version_data = install_profile["versionInfo"]
            jar_path = install_profile.get("install", {}).get("filePath")
            if jar_path:
                jar_path = jar_path.replace("/", "", 1)
                lib_path = self.game_path / "libraries" / f"{Libs.name_to_path(install_profile['install']['path'])}"
                lib_path.parent.mkdir(parents=True, exist_ok=True)
                lib_path.write_bytes(zf.read(jar_path))
            else:
                jar_path = Libs.name_to_path(install_profile["path"])
                lib_path = self.game_path / "libraries" / f"{jar_path}"
                lib_path.parent.mkdir(parents=True, exist_ok=True)
                lib_path.write_bytes(zf.read(f"maven/{jar_path}"))
        (self.game_path / "versions" / save_name / f"{save_name}.json").write_text(json.dumps(version_data, ensure_ascii=False, indent=4), encoding="utf-8")
        return True

