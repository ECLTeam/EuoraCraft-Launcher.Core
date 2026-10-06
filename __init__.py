# ============================================================
# EuoraCraft Launcher Core
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher.Core
#
# 文件作用：Core 的稳定公开接口，主仓库只允许经本模块导入 Core 能力。
#
# 公开接口：
#   - 见 __all__；新增能力必须同时加入 __all__ 才能被主仓库使用。
# ============================================================

from .Core.Downloader import Downloader, DynamicSemaphore, RateLimiter
from .Core.ECLauncherCore import LaunchConfig, build_minecraft_cmd
from .Core.Errors import CoreError
from .Core.FilesChecker import FilesChecker
from .Core.GetGames import GetGames, VersionClassifier
from .Core.InstancesManager import InstancesManager
from .Core.Libs import atomic_write_text, find_version, get_file_sha1, name_to_path, name_to_uuid, unzip
from .Core.LoaderInstaller import LoaderInstaller
from .Core.MicrosoftAuth import AuthException, MicrosoftAuthManager, NetException
from .Core.NetLibs import ApiUrlConfig, BaseApiClient, BmclApiUrl
from .Core.YggdrasilAuth import YggdrasilClient as OriginalYggdrasilClient
from .Utils.JavaScanner import JavaRuntime, JavaScanner
from .Utils.SearchMinecraft import SearchMinecraft


class YggdrasilClient(OriginalYggdrasilClient):
    """
    扩展原始 Yggdrasil 客户端的刷新接口。

    原始请求流程保持不变，仅允许调用方在刷新令牌时绑定已选择的角色。
    """

    def refresh(
        self,
        url: str,
        access_token: str,
        client_token: str,
        follow_ali: bool = True,
        selected_profile: dict[str, str] | None = None,
    ) -> dict:
        """
        刷新令牌并可选绑定指定角色。

        :param url: Yggdrasil API 地址
        :param access_token: 当前访问令牌
        :param client_token: 当前客户端令牌
        :param follow_ali: 是否解析 Authlib-Injector API Location
        :param selected_profile: 本次登录选择的角色
        :return: Yggdrasil 刷新响应
        """
        if selected_profile is None:
            return super().refresh(url, access_token, client_token, follow_ali)
        root_url = self.follow_ali(url) if follow_ali else url.strip("/")
        response = self.client.post(
            f"{root_url}/authserver/refresh",
            json={
                "accessToken": access_token,
                "clientToken": client_token,
                "requestUser": True,
                "selectedProfile": selected_profile,
            },
        )
        response.raise_for_status()
        return response.json()

__all__ = [
    "ApiUrlConfig",
    "AuthException",
    "BaseApiClient",
    "BmclApiUrl",
    "CoreError",
    "Downloader",
    "DynamicSemaphore",
    "FilesChecker",
    "GetGames",
    "InstancesManager",
    "JavaRuntime",
    "JavaScanner",
    "LaunchConfig",
    "LoaderInstaller",
    "MicrosoftAuthManager",
    "NetException",
    "RateLimiter",
    "SearchMinecraft",
    "VersionClassifier",
    "YggdrasilClient",
    "atomic_write_text",
    "build_minecraft_cmd",
    "find_version",
    "get_file_sha1",
    "name_to_path",
    "name_to_uuid",
    "unzip",
]
