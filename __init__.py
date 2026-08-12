"""EuoraCraft Game 的稳定公开接口。"""

from .Core.Downloader import Downloader, DynamicSemaphore, RateLimiter
from .Core.ECLauncherCore import LaunchConfig, build_minecraft_cmd
from .Core.FilesChecker import FilesChecker
from .Core.GetGames import GetGames, VersionClassifier
from .Core.InstancesManager import InstancesManager
from .Core.Libs import find_version, get_file_sha1, name_to_path, name_to_uuid, unzip
from .Core.LoaderInstaller import LoaderInstaller
from .Core.MicrosoftAuth import MicrosoftAuthManager
from .Core.NetLibs import ApiUrlConfig, BaseApiClient, BmclApiUrl
from .facade import (
    CoreContext,
    CoreError,
    DownloadRequest,
    DownloadSource,
    Game,
    GameCore,
    InvalidCoreRequestError,
    LoaderType,
    ScanRequest,
    YggdrasilClient,
)
from .Utils.JavaScanner import JavaRuntime, JavaScanner
from .Utils.SearchMinecraft import SearchMinecraft

__all__ = [
    "ApiUrlConfig",
    "BaseApiClient",
    "BmclApiUrl",
    "CoreContext",
    "CoreError",
    "DownloadRequest",
    "DownloadSource",
    "Downloader",
    "DynamicSemaphore",
    "FilesChecker",
    "Game",
    "GameCore",
    "GetGames",
    "InstancesManager",
    "InvalidCoreRequestError",
    "JavaRuntime",
    "JavaScanner",
    "LaunchConfig",
    "LoaderInstaller",
    "LoaderType",
    "MicrosoftAuthManager",
    "RateLimiter",
    "ScanRequest",
    "SearchMinecraft",
    "VersionClassifier",
    "YggdrasilClient",
    "build_minecraft_cmd",
    "find_version",
    "get_file_sha1",
    "name_to_path",
    "name_to_uuid",
    "unzip",
]
