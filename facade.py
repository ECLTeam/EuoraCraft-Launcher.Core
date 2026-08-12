from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from threading import RLock
from typing import Any

from .Core.Downloader import Downloader, DynamicSemaphore
from .Core.ECLauncherCore import LaunchConfig, build_minecraft_cmd
from .Core.FilesChecker import FilesChecker
from .Core.GetGames import GetGames
from .Core.InstancesManager import InstancesManager
from .Core.LoaderInstaller import LoaderInstaller
from .Core.NetLibs import ApiUrlConfig, BaseApiClient, BmclApiUrl
from .Core.YggdrasilAuth import YggdrasilClient as OriginalYggdrasilClient
from .Utils.SearchMinecraft import SearchMinecraft


class CoreError(Exception):
    """表示 Game 公开边界返回的基础错误。"""


class InvalidCoreRequestError(CoreError):
    """表示传给 Game 公开边界的请求不满足约束。"""


class DownloadSource(StrEnum):
    """Game 支持的元数据与文件下载源。"""

    OFFICIAL = "official"
    BMCLAPI = "bmclapi"


class LoaderType(StrEnum):
    """Game 支持安装的加载器类型。"""

    FABRIC = "fabric"
    FORGE = "forge"
    NEOFORGE = "neoforge"
    QUILT = "quilt"


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


@dataclass(frozen=True, slots=True)
class ScanRequest:
    """
    描述一次本地 Minecraft 目录扫描请求。

    :param game_path: 待扫描的 ``.minecraft`` 根目录
    """

    game_path: Path


@dataclass(frozen=True, slots=True)
class DownloadRequest:
    """
    描述一组文件下载任务及其并发、限速配置。

    :param entries: 由下载地址和保存路径组成的任务元组
    :param concurrency: 同时执行的最大下载任务数
    :param speed_limit_mb: 全局下载速度上限，零表示不限速
    """

    entries: tuple[tuple[str, str], ...]
    concurrency: int = 64
    speed_limit_mb: float = 0

    @classmethod
    def from_entries(
        cls,
        entries: Iterable[tuple[str, str]],
        *,
        concurrency: int = 64,
        speed_limit_mb: float = 0,
    ) -> DownloadRequest:
        """
        从可迭代下载任务创建不可变请求。

        :param entries: 由下载地址和保存路径组成的任务
        :param concurrency: 同时执行的最大下载任务数
        :param speed_limit_mb: 全局下载速度上限，零表示不限速
        :return: 可交给 ``Game.downloader`` 的下载请求
        """
        return cls(tuple(entries), concurrency, speed_limit_mb)


@dataclass(frozen=True, slots=True)
class CoreContext:
    """
    保存一个游戏目录对应的原始 Game 对象组合。

    :param api_client: Game 原生网络客户端
    :param files: Game 原生文件校验器
    :param catalog: Game 原生版本目录与安装入口
    """

    api_client: BaseApiClient
    files: FilesChecker
    catalog: GetGames


class Game:
    """为原始 ``Core``/``Utils`` 实现提供稳定的轻量门面。"""

    def __init__(
        self,
        *,
        api_client_factory: Callable[[ApiUrlConfig], BaseApiClient] = BaseApiClient,
        instances: InstancesManager | None = None,
    ) -> None:
        """
        创建门面并准备按游戏目录延迟构造原始对象。

        :param api_client_factory: 创建 Game 原生网络客户端的工厂
        :param instances: 可选的原始游戏实例管理器
        """
        self.instances = instances or InstancesManager()
        self._api_client_factory = api_client_factory
        self._contexts: dict[tuple[str, DownloadSource], CoreContext] = {}
        self._lock = RLock()
        self._closed = False

    def context(self, game_path: str | Path, source: DownloadSource = DownloadSource.OFFICIAL) -> CoreContext:
        """
        获取或创建一个游戏目录对应的原始 Game 对象组合。

        :param game_path: ``.minecraft`` 根目录
        :param source: 元数据与文件下载源
        :return: 可复用的网络、文件校验和版本目录对象
        """
        path = Path(game_path).expanduser().resolve(strict=False)
        key = (str(path).casefold(), source)
        with self._lock:
            if self._closed:
                raise CoreError("Game 已关闭")
            if key in self._contexts:
                return self._contexts[key]
            client = self._api_client_factory(BmclApiUrl() if source is DownloadSource.BMCLAPI else ApiUrlConfig())
            files = FilesChecker(client)
            installer = LoaderInstaller(files, self.instances, path)
            context = CoreContext(client, files, GetGames(files, installer, path))
            self._contexts[key] = context
            return context

    def scan(self, request: ScanRequest) -> dict[str, dict[str, Any]]:
        """
        使用原始搜索器扫描一个 Minecraft 目录。

        :param request: 包含目标目录的扫描请求
        :return: 以版本目录名称为键的扫描结果
        """
        return SearchMinecraft(request.game_path).search_minecraft()

    @staticmethod
    def downloader(
        request: DownloadRequest,
        *,
        progress: Callable[[int, int], None] | None = None,
    ) -> Downloader:
        """
        根据类型化请求创建原始下载器。

        :param request: 下载任务、并发数和限速配置
        :param progress: 可选的下载进度回调
        :return: 已应用并发配置的原始下载器
        """
        if request.concurrency < 1:
            raise InvalidCoreRequestError("concurrency 必须大于零")
        downloader = Downloader(
            list(request.entries),
            speed_limit_mb=request.speed_limit_mb,
            progress_callback=progress,
        )
        downloader.concurrency = request.concurrency
        downloader.semaphore = DynamicSemaphore(request.concurrency)
        return downloader

    @staticmethod
    def build_launch_command(config: LaunchConfig) -> str:
        """
        使用原始 Game 实现生成 Minecraft 启动命令。

        :param config: Game 原生启动配置
        :return: 可交给实例管理器执行的命令
        """
        return build_minecraft_cmd(config)

    def close(self) -> None:
        """关闭门面创建的进程和原始网络客户端。"""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            contexts = tuple(self._contexts.values())
            self._contexts.clear()
        self.instances.shutdown_all(force=False)
        for context in contexts:
            context.api_client.close()


GameCore = Game


__all__ = [
    "CoreContext",
    "CoreError",
    "DownloadRequest",
    "DownloadSource",
    "Game",
    "GameCore",
    "InvalidCoreRequestError",
    "LoaderType",
    "ScanRequest",
    "YggdrasilClient",
]
