from collections import deque
from .InstallProgress import InstallObserver
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

# 处理器参数里的 {占位符}，取值来自安装器 data 声明与 ECL 内置变量。
_processor_token_pattern = re.compile(r"\{([A-Z0-9_]+)\}")


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
        save_name: str,
        *, observer: InstallObserver = InstallObserver(),
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
                bin_entry = install_profile["data"]["BINPATCH"]["client"]
                bin_name = bin_entry.replace("[", "").replace("]", "").replace("'", "").replace("/", "", 1)
                patch_bin = zf.read(bin_name)
        except (zipfile.BadZipFile, OSError) as exc:
            raise RuntimeError(f"安装器文件无法读取：{installer_path}") from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"安装器清单不是有效 JSON：{installer_path}") from exc
        except KeyError as exc:
            # 清单缺少 BINPATCH 说明这不是 NeoForge 安装器，交给 Forge 流程；
            # 其余缺失（如压缩包内没有补丁文件）属于文件损坏，必须显式失败，
            # 否则会被当成「需要走 Forge 流程」而装出错误的版本。
            if exc.args and exc.args[0] in {"BINPATCH", "data"}:
                return self.install_forge(installer_path, save_name, observer=observer)
            raise RuntimeError(f"安装器缺少必要文件：{installer_path}（{exc}）") from exc
        if "processors" not in install_profile or not install_profile["processors"]:
            return self.install_forge(installer_path, save_name, observer=observer)
        java_path = Path(java_path)
        ver_path = self.game_path / "versions" / save_name
        bin_path = installer_path.parent / "client.bin"
        bin_path.write_bytes(patch_bin)

        observer.emit("download_loader_libraries", action="checking")
        download_list = self.files_checker.check_libraries(self.game_path, install_profile, observer=observer)
        download_list.extend(self.files_checker.check_libraries(self.game_path, version_data, observer=observer))
        if download_list:
            downloader = Downloader(download_list, skip_preflight=True, observer=observer,
                                    stage="download_loader_libraries")
            asyncio.run(downloader.run())
            observer.check_cancelled()
            if downloader.failed_entries:
                raise RuntimeError("加载器依赖下载失败")
            observer.emit("download_loader_libraries", status="completed")
        else:
            observer.emit("download_loader_libraries", status="skipped")
        observer.emit("install_loader")

        patched_name = install_profile["data"]["PATCHED"]["client"].replace("[", "").replace("]", "").replace("'", "")
        patched_path = self.game_path / "libraries" / f"{Libs.name_to_path(patched_name)}"
        patched_path.parent.mkdir(parents=True, exist_ok=True)

        processors = []
        tokens = self._installer_tokens(
            install_profile, version_data, installer_path, bin_path, patched_path, ver_path
        )
        for processor in install_profile["processors"]:
            if "sides" in processor and "client" not in processor["sides"]:
                continue
            processors.append(self._processor_command(processor, tokens, java_path))

        for index, command in enumerate(processors, 1):
            observer.emit("install_loader", unit="items", completed=index - 1, total=len(processors))
            # 保留最后若干行输出，处理器失败时随异常一起抛出，便于定位是哪个步骤出错。
            output_tail: deque[str] = deque(maxlen=20)

            def report(line: str, _instance_id: str) -> None:
                output_tail.append(line)
                self.log_callback(line)

            _, process = self.instances_mgr.create_instance(
                instance_name="NeoForged Installer",
                instance_type="LoaderInstaller",
                args=command,
                cwd=installer_path.parent,
                log_callback=report,
                block_thread=True
            )
            if process.returncode != 0:
                detail = " | ".join(item.strip() for item in output_tail if item.strip())
                raise RuntimeError(
                    f"加载器安装处理失败：第 {index}/{len(processors)} 个处理器退出码 {process.returncode}"
                    + (f"；最后输出：{detail}" if detail else "")
                )
            observer.emit("install_loader", unit="items", completed=index, total=len(processors))

        # 处理器全部成功后才写版本描述：中途失败不会留下能被当成「已安装」的半成品。
        ver_path.mkdir(parents=True, exist_ok=True)
        (ver_path / f"{save_name}.json").write_text(json.dumps(version_data, ensure_ascii=False, indent=4), encoding="utf-8")
        observer.emit("install_loader", status="completed")
        return True

    def _installer_tokens(
        self,
        install_profile: dict,
        version_data: dict,
        installer_path: Path,
        bin_path: Path,
        patched_path: Path,
        ver_path: Path,
    ) -> dict[str, str]:
        """
        汇总处理器参数可用的全部占位符。

        先按安装器 data 声明解析（``[group:artifact:version[:classifier][@ext]]``
        指向 libraries 内的构件，``'文本'`` 按字面量使用），再叠加 ECL 内置占位符。
        BINPATCH 必须覆盖为已解压的补丁文件：处理器读不到安装器压缩包内的路径。
        """
        tokens: dict[str, str] = {}
        for key, entry in (install_profile.get("data") or {}).items():
            if isinstance(entry, dict):
                raw = entry.get("client") or entry.get("server")
            else:
                raw = entry
            if isinstance(raw, str):
                tokens[str(key)] = self._resolve_installer_value(raw)
        tokens.update({
            "MINECRAFT_JAR": str(ver_path / f"{version_data.get('inheritsFrom', 'None')}.jar"),
            "MINECRAFT_VERSION": str(version_data.get("inheritsFrom")),
            "INSTALLER": str(installer_path),
            "SIDE": "client",
            "ROOT": str(self.game_path),
            "LIBRARY_DIR": str(self.game_path / "libraries"),
            "PATCHED": str(patched_path),
            "BINPATCH": str(bin_path),
        })
        return tokens

    def _resolve_installer_value(self, value: str) -> str:
        """
        把安装器 data 中的一个取值解析为处理器可直接使用的字符串。

        ``[group:artifact:version[:classifier][@ext]]`` 是构件坐标，落到 libraries
        下的绝对路径；``'文本'`` 是字面量；其余原样返回。坐标无法解析时保留原文，
        交给处理器报错，避免静默指向一个不存在的目录。
        """
        text = str(value).strip()
        if text.startswith("[") and text.endswith("]"):
            relative = Libs.name_to_path(text[1:-1])
            return str(self.game_path / "libraries" / relative) if relative else text
        if len(text) >= 2 and text.startswith("'") and text.endswith("'"):
            return text[1:-1]
        return text

    def _processor_command(self, processor: dict, tokens: dict[str, str], java_path: Path) -> list[str]:
        """
        生成单个处理器的完整命令行。

        以参数列表形式交给子进程，避免路径含空格时被字符串拼接和手写引号破坏；
        参数中的未知占位符直接报错，而不是原样传给 Java 得到难以定位的失败。
        """
        classpath = [
            str(self.game_path / "libraries" / Libs.name_to_path(name))
            for name in processor["classpath"]
        ]
        jar_path = self.game_path / "libraries" / Libs.name_to_path(processor["jar"])
        if processor["jar"] not in processor["classpath"]:
            classpath.append(str(jar_path))
        with zipfile.ZipFile(jar_path, "r") as zf:
            manifest_mf = zf.read("META-INF/MANIFEST.MF").decode("utf-8")
        match = re.search(r"^Main-Class:\s*(.+)$", manifest_mf, re.MULTILINE)
        if not match:
            raise RuntimeError(f"安装器处理器缺少 Main-Class：{jar_path}")
        main_class = match.group(1).strip()

        expanded = []
        for raw in processor["args"]:
            value = _processor_token_pattern.sub(lambda m: self._token_value(m, tokens), str(raw))
            # 参数里也会直接出现 [坐标] 与 '字面量'，与 data 取值同一套解析规则。
            expanded.append(self._resolve_installer_value(value))
        return [str(java_path), "-cp", self.cp_delimiter.join(classpath), main_class, *expanded]

    @staticmethod
    def _token_value(match: re.Match, tokens: dict[str, str]) -> str:
        """取占位符对应的取值，缺失时立刻失败而不是把占位符传给 Java。"""
        name = match.group(1)
        if name not in tokens:
            raise RuntimeError(f"安装器参数包含未知占位符：{{{name}}}")
        return tokens[name]

    def install_forge(
        self,
        installer_path: Path | str,
        save_name: str,
        *, observer: InstallObserver = InstallObserver(),
    ) -> bool:
        """
        安装旧版 Forge
        :param installer_path: Installer.jar 路径
        :param save_name: 保存名称
        :return: bool
        """
        observer.emit("download_loader_libraries", status="skipped")
        observer.emit("install_loader")
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
        observer.emit("install_loader", status="completed")
        return True

