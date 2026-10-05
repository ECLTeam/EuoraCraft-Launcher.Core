from dataclasses import dataclass
from pathlib import Path
import subprocess
import hashlib
import json
import sys
import os
import re

# Windows 注册表支持（仅 Windows）
try:
    import winreg
except ImportError:
    winreg = None


@dataclass
class JavaRuntime:
    """表示一个 Java 运行时的数据类"""
    path: Path                # java 可执行文件绝对路径
    version: str              # 版本号（如 "17.0.3"）
    vendor: str | None = None
    architecture: str = "Unknown"
    is_jdk: bool = False


class JavaScanner:
    """
    跨平台 Java 扫描器
    支持：
      - Windows / Linux / macOS 常见安装目录
      - 兼容架构搜索（x86、x64、ARM64）
      - 注册表查询（Windows）
      - PATH 环境变量
      - 用户手动添加/禁用列表
      - 缓存加速（基于文件特征）
      - 自动添加当前 JVM
    """

    KNOWN_VENDOR_DIRS = ["Java", "BellSoft", "AdoptOpenJDK", "Zulu", "Microsoft", "Eclipse Foundation", "Semeru"]

    def __init__(self,
                 cache_file: Path | str | None = None,
                 user_java_paths: list[str] | None = None,
                 disabled_java_paths: list[str] | None = None):
        """
        :param cache_file: 缓存文件路径，默认 ~/.java_scanner_cache.json
        :param user_java_paths: 用户手动添加的 java 可执行文件路径列表（字符串）
        :param disabled_java_paths: 用户禁用的 java 路径列表（字符串）
        """
        self.cache_file = Path(cache_file) if cache_file else Path.home() / ".ECL" / "java_cache.json"
        self.user_java_paths = user_java_paths or []
        self.disabled_paths = set(Path(p).resolve() for p in (disabled_java_paths or []) if p)

        self._found: dict[Path, JavaRuntime] = {}
        self._failed: set[Path] = set()
        self._cache_data: dict[str, dict] = {}   # str(Path) -> {"key": str, "info": dict}
        self._need_refresh_cache = False
        self._load_cache()

    # ---------- 公共扫描接口 ----------
    def scan(self) -> list[JavaRuntime]:
        """执行完整扫描，返回所有可用的 JavaRuntime 列表（已去重）"""
        self._found.clear()
        self._failed.clear()

        # 1. 扫描系统常见路径（含兼容架构）
        current_os = sys.platform
        if current_os.startswith("win"):
            self._scan_windows()
        elif current_os.startswith("linux"):
            self._scan_linux()
        elif current_os.startswith("darwin"):
            self._scan_macos()

        # 2. 扫描 PATH
        self._scan_path()

        # 3. 添加用户手动添加的路径
        for path_str in self.user_java_paths:
            try:
                self._try_add_java(Path(path_str))
            except Exception:
                pass

        # 4. 添加当前运行的 Java（如果尚未被扫描到且未被禁用）
        self._add_current_java()

        # 5. 保存缓存
        self._save_cache()

        return list(self._found.values())

    # ---------- 缓存 ----------
    @staticmethod
    def probe(executable: Path | str) -> JavaRuntime | None:
        """
        有界探测一个 Java 可执行文件，不扫描系统或修改扫描缓存。

        :param executable: Java 或 javaw 的可执行路径
        :return: 成功校验的运行时；文件不存在、退出失败或信息不完整时返回 None
        """
        path = Path(executable).expanduser().resolve(strict=False)
        if not path.is_file() or path.name.lower() not in {"java", "java.exe", "javaw.exe"}:
            return None
        probe_path = path.with_name("java.exe") if path.name.lower() == "javaw.exe" else path
        if not probe_path.is_file():
            return None
        info = JavaScanner._extract_java_info(probe_path)
        if info is None:
            return None
        return JavaRuntime(path, info["version"], info.get("vendor"), info["architecture"], info["is_jdk"])

    def _load_cache(self):
        if self.cache_file.is_file():
            try:
                with open(self.cache_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self._cache_data = data.get("caches", {})
            except Exception:
                self._cache_data = {}

    def _save_cache(self):
        if not self._need_refresh_cache:
            return
        try:
            with open(self.cache_file, "w", encoding="utf-8") as f:
                json.dump({"caches": self._cache_data}, f, indent=2, default=str)
            self._need_refresh_cache = False
        except Exception:
            pass

    def _make_cache_key(self, exe_path: Path) -> str | None:
        """生成缓存键：基于文件大小、修改时间，以及 release 文件或 rt.jar 的特征"""
        try:
            stat = exe_path.stat()
            parts = ["probe:2", f"sz:{stat.st_size}", f"lm:{stat.st_mtime_ns}"]
            java_home = exe_path.parent.parent
            release_file = java_home / "release"
            if release_file.is_file():
                sha1 = hashlib.sha1(release_file.read_bytes()).hexdigest()
                parts.append(sha1)
            else:
                for rt in [java_home / "lib" / "rt.jar", java_home / "jre" / "lib" / "rt.jar"]:
                    if rt.is_file():
                        st = rt.stat()
                        parts.append(f"rsz:{st.st_size}")
                        parts.append(f"rlm:{st.st_mtime_ns}")
                        break
            return ",".join(parts)
        except Exception:
            return None

    def _get_cached_info(self, exe_path: Path, cache_key: str) -> dict | None:
        cached = self._cache_data.get(str(exe_path))
        if cached and cached.get("key") == cache_key:
            return cached.get("info")
        return None

    def _update_cache(self, exe_path: Path, cache_key: str, info: dict):
        self._cache_data[str(exe_path)] = {"key": cache_key, "info": info}
        self._need_refresh_cache = True

    # ---------- 添加 Java 运行时（核心） ----------
    def _try_add_java(self, executable: Path):
        """尝试添加一个 Java 可执行文件（路径可以是相对路径，会解析为绝对路径）"""
        try:
            real = executable.resolve(strict=True)
        except (OSError, RuntimeError):
            return

        if real in self._found or real in self._failed:
            return

        # 检查禁用列表
        if real in self.disabled_paths:
            return

        # 尝试缓存
        cache_key = self._make_cache_key(real)
        if cache_key:
            cached_info = self._get_cached_info(real, cache_key)
            if cached_info:
                self._found[real] = JavaRuntime(
                    path=real,
                    version=cached_info["version"],
                    vendor=cached_info.get("vendor"),
                    architecture=cached_info.get("architecture", "unknown"),
                    is_jdk=cached_info.get("is_jdk", False),
                )
                return

        # 执行 java -XshowSettings:properties -version
        info = self._extract_java_info(real)
        if info is None:
            self._failed.add(real)
            return

        if cache_key:
            self._update_cache(real, cache_key, info)

        self._found[real] = JavaRuntime(
            path=real,
            version=info["version"],
            vendor=info.get("vendor"),
            architecture=info.get("architecture", "unknown"),
            is_jdk=info.get("is_jdk", False),
        )

    def _try_add_java_home(self, java_home: Path):
        """通过 JAVA_HOME 目录添加（自动拼接 bin/java）"""
        exe = java_home / "bin" / self._java_exe_name()
        self._try_add_java(exe)

    def _scan_directory(self, root: Path, recursive: bool = True):
        """扫描目录下的所有子目录（或仅一层）作为 JavaHome"""
        if not root.is_dir():
            return
        for child in root.iterdir():
            if child.is_dir():
                self._try_add_java_home(child)

    # ---------- 平台特定扫描 ----------
    def _scan_windows(self):
        # 注册表
        if winreg is not None:
            subkeys = [
                r"SOFTWARE\JavaSoft\Java Runtime Environment",
                r"SOFTWARE\JavaSoft\Java Development Kit",
                r"SOFTWARE\JavaSoft\JRE",
                r"SOFTWARE\JavaSoft\JDK"
            ]
            for sub in subkeys:
                self._query_registry(winreg.HKEY_LOCAL_MACHINE, sub)

        # Program Files
        pf = os.environ.get("ProgramFiles", "C:\\Program Files")
        pf86 = os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)")
        # 搜索当前架构
        for vendor in self.KNOWN_VENDOR_DIRS:
            self._scan_directory(Path(pf) / vendor, recursive=False)
        # 兼容架构：当前 x64 搜索 x86，当前 ARM64 搜索 x86 和 x64（若支持翻译）
        arch = self._get_system_arch()
        if arch == "x86_64":
            for vendor in self.KNOWN_VENDOR_DIRS:
                self._scan_directory(Path(pf86) / vendor, recursive=False)
        elif arch in ("arm64", "aarch64"):
            # 假设系统支持 x86/x64 翻译（此处简单都搜）
            for vendor in self.KNOWN_VENDOR_DIRS:
                self._scan_directory(Path(pf86) / vendor, recursive=False)
                self._scan_directory(Path(pf) / vendor, recursive=False)

    def _scan_linux(self):
        dirs = [
            "/usr/java",
            "/usr/lib/jvm",
            "/usr/lib32/jvm",
            "/usr/lib64/jvm",
            str(Path.home() / ".sdkman/candidates/java"),
            str(Path.home() / ".jdks"),
        ]
        for d in dirs:
            self._scan_directory(Path(d), recursive=False)

    def _scan_macos(self):
        # /Library/Java/JavaVirtualMachines 和 ~/Library/Java/JavaVirtualMachines
        for base in ["/Library/Java/JavaVirtualMachines", str(Path.home() / "Library/Java/JavaVirtualMachines")]:
            self._scan_mac_jvm_folder(Path(base))

        # Homebrew
        homebrew = Path("/opt/homebrew")
        if homebrew.is_dir():
            # Cellar/openjdk*
            for child in (homebrew / "Cellar").glob("openjdk*"):
                if child.is_dir():
                    self._scan_directory(child, recursive=False)
            self._try_add_java(homebrew / "opt/java/bin/java")

        # Apple 插件
        self._try_add_java(Path("/Library/Internet Plug-Ins/JavaAppletPlugin.plugin/Contents/Home/bin/java"))
        # Xcode
        self._try_add_java(Path("/Applications/Xcode.app/Contents/Applications/Application Loader.app/Contents/MacOS/itms/java/bin/java"))

    def _scan_mac_jvm_folder(self, root: Path):
        """扫描 macOS JVM 目录：/.../jdk-xxx/Contents/Home"""
        if not root.is_dir():
            return
        for sub in root.iterdir():
            if sub.is_dir():
                home = sub / "Contents" / "Home"
                if home.is_dir():
                    self._try_add_java_home(home)

    # ---------- 其他扫描方式 ----------
    def _scan_path(self):
        path_env = os.environ.get("PATH", "")
        for p in path_env.split(os.pathsep):
            if not p:
                continue
            if sys.platform.startswith("win") and "\\common files\\oracle\\java\\" in p.lower():
                continue
            try:
                self._try_add_java(Path(p) / self._java_exe_name())
            except Exception:
                pass

    def _add_current_java(self):
        """添加当前 JVM 的 java（如果尚未被发现且未被禁用）"""
        java_home = os.environ.get("JAVA_HOME")
        if java_home:
            exe = Path(java_home) / "bin" / self._java_exe_name()
            if exe not in self._found and exe not in self._failed:
                self._try_add_java(exe)

    # ---------- Windows 注册表 ----------
    def _query_registry(self, hkey, subkey):
        if winreg is None:
            return
        try:
            key = winreg.OpenKey(hkey, subkey, 0, winreg.KEY_READ)
            index = 0
            while True:
                try:
                    name = winreg.EnumKey(key, index)
                    index += 1
                    # 检查是否存在子键 {name}\MSI 作为有效标志
                    try:
                        winreg.OpenKey(key, f"{name}\\MSI")
                    except FileNotFoundError:
                        continue
                    try:
                        sub_key = winreg.OpenKey(key, name)
                        home, _ = winreg.QueryValueEx(sub_key, "JavaHome")
                        if home:
                            self._try_add_java_home(Path(home))
                    except Exception:
                        pass
                except OSError:
                    break
        except Exception:
            pass

    # ---------- 辅助方法 ----------
    @staticmethod
    def _java_exe_name() -> str:
        return "java.exe" if sys.platform.startswith("win") else "java"

    @staticmethod
    def _get_system_arch() -> str:
        """获取系统架构（统一为 x86, x86_64, arm64）"""
        arch = os.environ.get("PROCESSOR_ARCHITECTURE", "").lower()
        if arch in ("amd64", "x86_64"):
            return "x86_64"
        elif arch in ("x86", "i386", "i686"):
            return "x86"
        elif arch in ("arm64", "aarch64"):
            return "arm64"
        # fallback
        import platform
        return platform.machine().lower()

    @staticmethod
    def _extract_java_info(java_exe: Path) -> dict | None:
        """
        执行 java -XshowSettings:properties -version，解析输出
        返回字典：version, vendor, architecture, is_jdk
        """
        try:
            proc = subprocess.run(
                [str(java_exe), "-XshowSettings:properties", "-version"],
                capture_output=True,
                text=True,
                timeout=10,
                encoding="utf-8",
                errors="ignore",
                **({"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform.startswith("win") else {})
            )
            if proc.returncode != 0:
                return None
            output = proc.stderr if proc.stderr else proc.stdout
            if not output:
                return None

            props = {}
            for line in output.splitlines():
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    props[k.strip()] = v.strip()

            info = {
                "version": props.get("java.version", "unknown"),
                "vendor": props.get("java.vendor"),
                "architecture": props.get("os.arch", "unknown"),
                "is_jdk": False,
            }
            if not re.match(r"^\d+(?:\.\d+)*", info["version"]) or info["architecture"] == "unknown":
                return None
            java_home: str | None = props.get("java.home")
            if java_home:
                home = Path(java_home)
                compiler = "javac.exe" if sys.platform.startswith("win") else "javac"
                if (java_exe.parent / compiler).is_file() or (home / "bin" / compiler).is_file():
                    info["is_jdk"] = True
            return info
        except Exception:
            return None


# ---------- 使用示例 ----------
if __name__ == "__main__":
    scanner = JavaScanner(
        user_java_paths=["C:/custom/java/bin/java.exe"],
        disabled_java_paths=["C:/broken/java/bin/java.exe"]
    )
    runtimes = scanner.scan()
    print(f"Found {len(runtimes)} Java runtimes:")
    for rt in runtimes:
        print(f"  {rt.path}")
        print(f"    Version: {rt.version}")
        print(f"    Vendor: {rt.vendor}")
        print(f"    Arch: {rt.architecture}")
        print(f"    JDK: {rt.is_jdk}")
        print()
