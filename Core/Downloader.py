from typing import Callable
from pathlib import Path
import threading
import aiofiles
import asyncio
import queue
import httpx
import math
import time


class DynamicSemaphore:
    """
    可动态调整上限的异步信号量。

    用于控制并发协程数量，支持运行时修改上限值（change 方法）。
    内部使用 asyncio.Condition 实现等待/通知机制，线程安全。

    方法：
        acquire(): 获取一个许可，若当前许可数为 0 则阻塞等待。
        release(): 释放一个许可，唤醒一个等待者。
        change(new_value): 动态调整许可总数，若增大则立即唤醒等待任务。
    """

    def __init__(self, value: int):
        """
        初始化信号量。

        :param value: 初始许可数量。
        """
        self._value = value
        self._condition = asyncio.Condition()

    async def acquire(self):
        """获取一个许可，若无可用则阻塞。"""
        async with self._condition:
            while self._value <= 0:
                await self._condition.wait()
            self._value -= 1

    def release(self):
        """释放一个许可，唤醒一个等待者（异步触发）。"""
        async def _release():
            async with self._condition:
                self._value += 1
                self._condition.notify()
        asyncio.create_task(_release())

    def change(self, new_value: int):
        """
        动态调整许可总数。若增大，则唤醒阻塞的等待任务。

        :param new_value: 新的许可总数。
        """
        async def _change():
            async with self._condition:
                delta = new_value - self._value
                if delta > 0:
                    self._value = new_value
                    self._condition.notify_all()
                else:
                    self._value = new_value
        asyncio.create_task(_change())

    @property
    def value(self) -> int:
        """当前许可数量。"""
        return self._value


class RateLimiter:
    """
    基于固定时间窗口的令牌桶限速器。

    通过限制每个时间窗口内通过的字节数来控制下载速度。
    窗口大小默认为 0.1 秒，通过 acquire(bytes_to_send) 请求允许发送指定字节数，
    若窗口内累计字节超过限制，则等待至下一窗口。
    """

    def __init__(self, speed_limit_mb: float, window: float = 0.1):
        """
        初始化限速器。

        :param speed_limit_mb: 每秒允许的最大字节数（MB/s），0 表示不限速。
        :param window: 时间窗口长度（秒）。
        """
        self.speed_limit_bytes = speed_limit_mb * 1024 * 1024
        self.window = window
        self.max_bytes_per_window = self.speed_limit_bytes * window
        self.current_window_bytes = 0
        self.window_start = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, bytes_to_send: int):
        """
        请求允许发送 bytes_to_send 字节。
        若当前窗口内累计字节超过限制，则等待至下一窗口。

        :param bytes_to_send: 欲发送的字节数。
        """
        if self.speed_limit_bytes == 0:
            return
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self.window_start
            if elapsed >= self.window:
                self.window_start = now
                self.current_window_bytes = 0
            else:
                if self.current_window_bytes + bytes_to_send > self.max_bytes_per_window:
                    wait_time = self.window - elapsed
                    await asyncio.sleep(wait_time)
                    self.window_start = time.monotonic()
                    self.current_window_bytes = 0
            self.current_window_bytes += bytes_to_send


class Downloader:
    """
    主下载器类，支持多任务并发下载、限速、进度回调、自动重试、暂停/恢复以及智能分片。
    """

    def __init__(
        self,
        download_list: list[tuple[str, Path | str]],
        speed_limit_mb: float = 0.0,
        progress_callback: Callable[[int, int], None] | None = None,
        speed_callback: Callable[[float], None] | None = None,
        max_rounds: int = 3,
        skip_preflight: bool = False,
        chunk_size_mb: int = 0,          # 0 表示自动计算
        max_chunks: int = 200,
        min_chunk_mb: int = 5,
        max_chunk_retries: int = 3,
        chunk_threshold_mb: float = 10.0  # 大于此值且支持 Range 才分片
    ):
        """
        初始化下载器。

        :param download_list: 下载任务列表，每个元素为 (url, path) 元组。
                               path 可以是字符串或 pathlib.Path 对象。
        :param speed_limit_mb: 全局速度限制（MB/s）。0 表示不限速。
                               限速模式下并发数固定为 200，不限速模式下使用自适应并发（初始 80，动态调节）。
        :param progress_callback: 进度回调函数，签名为 (downloaded: int, total: int) -> None。
                                   当下载进度更新时被调用。若所有文件能获取大小，则 downloaded 为已下载字节数，total 为总字节数；
                                   否则 downloaded 为已完成文件数，total 为总文件数。
        :param speed_callback: 速度回调函数，签名为 (speed_mb_per_sec: float) -> None。
                               每秒调用一次，报告当前实时下载速度（MB/s）。
        :param max_rounds: 最大重试轮数。每轮尝试下载所有未完成文件，失败的文件进入下一轮。
                           默认 3 轮，设为 0 表示不重试。
        :param skip_preflight: 是否跳过总文件大小预检查，若跳过则直接使用文件计数模式。
        :param chunk_size_mb: 目标分片大小（MB）。0 表示自动计算（根据文件大小和并发数动态调整）。
        :param max_chunks: 单个文件最大允许的分片数，防止过多分片。
        :param min_chunk_mb: 最小分片大小（MB），避免分片过小增加开销。
        :param max_chunk_retries: 单个分片下载失败时的最大重试次数。
        :param chunk_threshold_mb: 启用分片下载的文件大小阈值，单位为 MB。
                                   仅当文件大小大于此值，且服务器支持 Range 请求时，
                                   才会启用智能分片下载。默认为 10.0 MB。
                                   设为 0 或负数将禁用分片功能（即所有文件均不分片）。
        """
        # 统一路径类型，并去重
        seen_paths = set()
        self.original_downloads: list[tuple[str, Path]] = []
        for url, path in download_list:
            if not isinstance(path, Path):
                path = Path(path)
            path_str = str(path)
            if path_str not in seen_paths:
                seen_paths.add(path_str)
                self.original_downloads.append((url, path))

        self.speed_limit_mb = speed_limit_mb
        self.progress_callback = progress_callback or (lambda *args: None)
        self.speed_callback = speed_callback or (lambda *args: None)
        self.max_rounds = max_rounds
        self.skip_preflight = skip_preflight

        # 分片相关参数
        self.chunk_size_mb = chunk_size_mb
        self.max_chunks = max_chunks
        self.min_chunk_mb = min_chunk_mb
        self.max_chunk_retries = max_chunk_retries
        self.chunk_threshold_mb = chunk_threshold_mb

        # 状态存储
        self.completed_entries: set[tuple[str, str]] = set()
        self.failed_entries: set[tuple[str, str]] = set()
        self.pending_entries: list[tuple[str, Path, int | None]] = []

        # 进度相关
        self.total_bytes = 0          # 字节模式下为总字节数，文件模式下为总文件数
        self.downloaded_bytes = 0     # 字节模式下为已下载字节，文件模式下为已完成文件数
        self.total_files = 0          # 总文件数（两种模式均有效）
        self.use_byte_progress = True # 预检后决定
        self.bytes_downloaded_for_speed = 0  # 独立字节计数器，用于速度计算

        # 并发控制
        self.concurrency = 80 if speed_limit_mb == 0 else 200
        self.semaphore = DynamicSemaphore(self.concurrency)
        self.rate_limiter = RateLimiter(speed_limit_mb)

        self.loop = asyncio.get_running_loop() if asyncio._get_running_loop() else None
        self.pause_event = asyncio.Event()
        self.pause_event.set()  # 默认运行

        # 事件队列（异步 -> 同步）
        self.async_event_queue: asyncio.Queue[tuple[str, tuple] | None] = asyncio.Queue()
        self.sync_event_queue: queue.Queue[tuple[str, tuple] | None] = queue.Queue()
        self._dispatcher_thread: threading.Thread | None = None
        self._stop_dispatcher = threading.Event()

        self.headers = {"User-Agent": "EuoraCraft-Launcher"}
        self.client: httpx.AsyncClient | None = None

        # 速度统计
        self._last_downloaded = 0
        self._last_speed_time = time.monotonic()

    # ---------- 辅助方法 ----------
    def _is_entry_completed(self, url: str, path: Path) -> bool:
        """判断条目是否已完成。"""
        return (url, str(path)) in self.completed_entries

    def _is_entry_failed(self, url: str, path: Path) -> bool:
        """判断条目是否已标记为永久失败。"""
        return (url, str(path)) in self.failed_entries

    def _mark_completed(self, url: str, path: Path):
        """标记条目为已完成。"""
        entry = (url, str(path))
        if entry not in self.completed_entries:
            self.completed_entries.add(entry)

    def _mark_failed(self, url: str, path: Path):
        """标记条目为永久失败。"""
        entry = (url, str(path))
        self.failed_entries.add(entry)

    @staticmethod
    async def _head_one(client: httpx.AsyncClient, url: str, path: Path) -> tuple[str, Path, int]:
        """
        发送 HEAD 请求并返回 (url, path, content_length)，若失败则抛出异常。

        :param client: httpx.AsyncClient 实例。
        :param url: 文件 URL。
        :param path: 本地保存路径。
        :return: (url, path, content_length)
        """
        resp = await client.head(url)
        resp.raise_for_status()
        size = int(resp.headers.get("content-length", 0))
        return url, path, size

    # ---------- 预检 ----------
    async def _preflight(self):
        """
        预检所有文件，尝试获取 Content-Length。
        若全部成功且大小 > 0，使用字节进度模式；
        否则切换到文件计数模式，并保留已成功 HEAD 的结果（但后续进度按文件数走）。
        """
        total_files = len(self.original_downloads)
        self.total_files = total_files

        # 收集待检查的条目
        to_check = []
        for url, path in self.original_downloads:
            if self._is_entry_completed(url, path) or self._is_entry_failed(url, path):
                continue
            to_check.append((url, path))

        if not to_check:
            self.pending_entries = []
            return

        async with httpx.AsyncClient(http2=True, timeout=10.0, headers=self.headers, follow_redirects=True) as client:
            # 创建所有 HEAD 任务
            tasks = {}
            for url, path in to_check:
                task = asyncio.create_task(self._head_one(client, url, path))
                tasks[task] = (url, path)

            # 等待所有任务完成，收集结果
            results = await asyncio.gather(*tasks.keys(), return_exceptions=True)

            all_ok = True
            sizes = {}
            for res in results:
                if isinstance(res, Exception):
                    all_ok = False
                else:
                    url, path, size = res
                    sizes[(url, str(path))] = size
                    if size <= 0:
                        all_ok = False

        # 构建待下载列表
        self.pending_entries = []
        for url, path in self.original_downloads:
            if self._is_entry_completed(url, path) or self._is_entry_failed(url, path):
                continue
            key = (url, str(path))
            size = sizes.get(key, 0)
            # 若全部成功且所有大小 > 0，使用字节模式，并记录大小；否则使用文件模式，大小置 None
            if all_ok and all(s > 0 for s in sizes.values()):
                self.pending_entries.append((url, path, size))
            else:
                self.pending_entries.append((url, path, None))

        # 决定进度模式
        if all_ok and all(s > 0 for s in sizes.values()):
            self.use_byte_progress = True
            self.total_bytes = sum(sizes.values())
        else:
            self.use_byte_progress = False
            self.total_bytes = total_files  # 此时 total_bytes 代表总文件数

    # ---------- 智能分片参数计算 ----------
    def _calculate_chunk_params(self, file_size: int) -> tuple[int, int]:
        """
        根据文件大小、当前并发数和用户参数，智能计算分片大小（字节）和分片数量。

        :param file_size: 文件总字节数。
        :return: (实际分片大小（字节）, 分片数量)
        """
        if file_size <= 0:
            return 0, 0

        file_size_mb = file_size / (1024 * 1024)

        # 1. 确定目标分片大小（MB）
        if self.chunk_size_mb > 0:
            target_mb = self.chunk_size_mb
        else:
            # 自适应：文件越大，分片越大；但至少 min_chunk_mb，且不超过 50MB
            target_mb = max(self.min_chunk_mb, file_size_mb / 20)   # 例如 1GB -> 50MB
            target_mb = min(target_mb, 50)  # 上限 50MB

        # 2. 根据目标大小计算片数
        num_chunks = math.ceil(file_size / (target_mb * 1024 * 1024))

        # 3. 限制最大片数
        if num_chunks > self.max_chunks:
            num_chunks = self.max_chunks

        # 4. 考虑当前并发数：避免片数远大于并发数
        concurrency = self.concurrency
        if concurrency > 0 and num_chunks > concurrency * 2:
            # 使片数约为并发数的 2 倍，但不超过 max_chunks
            num_chunks = min(self.max_chunks, max(1, concurrency * 2))

        # 5. 重新计算实际分片大小
        actual_chunk_size = math.ceil(file_size / num_chunks)

        # 6. 确保分片大小不小于 min_chunk_mb
        min_chunk_bytes = self.min_chunk_mb * 1024 * 1024
        if actual_chunk_size < min_chunk_bytes:
            actual_chunk_size = min_chunk_bytes
            num_chunks = math.ceil(file_size / actual_chunk_size)
            if num_chunks > self.max_chunks:
                num_chunks = self.max_chunks
                actual_chunk_size = math.ceil(file_size / num_chunks)

        return actual_chunk_size, num_chunks

    # ---------- 分片下载相关 ----------
    async def _download_chunk(
        self,
        url: str,
        start: int,
        end: int,
        temp_chunk_path: Path,
    ) -> bool:
        """
        下载一个分片（字节范围 [start, end]）到临时分片文件。
        内部重试由调用方控制，此方法只尝试一次。

        :param url: 文件 URL。
        :param start: 起始字节。
        :param end: 结束字节（包含）。
        :param temp_chunk_path: 临时分片文件路径。
        :return: 成功返回 True，失败返回 False。
        """
        try:
            await self.semaphore.acquire()
            await self.pause_event.wait()

            headers = self.headers.copy()
            headers["Range"] = f"bytes={start}-{end}"
            async with self.client.stream(
                "GET", url, headers=headers, timeout=30.0
            ) as response:
                response.raise_for_status()
                if response.status_code != 206:
                    # 服务器不支持 Range，回退处理
                    return False

                temp_chunk_path.parent.mkdir(parents=True, exist_ok=True)
                async with aiofiles.open(temp_chunk_path, "wb") as f:
                    async for chunk in response.aiter_bytes(1 * 1024 * 1024):
                        await self.pause_event.wait()
                        await self.rate_limiter.acquire(len(chunk))
                        await f.write(chunk)
                        # 更新进度（仅字节模式）
                        if self.use_byte_progress:
                            self.downloaded_bytes += len(chunk)
                            progress = min(self.downloaded_bytes, self.total_bytes) if self.total_bytes > 0 else 0
                            self._put_event("progress", progress, self.total_bytes)
                        # 速度计数器
                        self.bytes_downloaded_for_speed += len(chunk)

            # 验证分片大小
            actual_size = temp_chunk_path.stat().st_size
            expected_size = end - start + 1
            if actual_size != expected_size:
                temp_chunk_path.unlink(missing_ok=True)
                return False

            return True
        except Exception:
            temp_chunk_path.unlink(missing_ok=True)
            return False
        finally:
            self.semaphore.release()

    @staticmethod
    async def _merge_chunks(chunk_paths: list[Path], target_path: Path):
        """
        异步合并分片文件到目标路径，完成后删除分片文件。

        :param chunk_paths: 分片文件路径列表（需按顺序）。
        :param target_path: 合并后的目标文件路径。
        """
        target_path.parent.mkdir(parents=True, exist_ok=True)

        def sync_merge():
            with target_path.open("wb") as out_f:
                for chunk_path in chunk_paths:
                    with chunk_path.open("rb") as in_f:
                        out_f.write(in_f.read())
                    chunk_path.unlink()

        await asyncio.to_thread(sync_merge)

    # ---------- 流式下载（不分片） ----------
    async def _download_stream(self, url: str, path: Path, expected_size: int | None = None) -> bool:
        """
        使用常规流式下载（不分片），用于小文件或不支持 Range 的场景。

        :param url: 文件 URL。
        :param path: 本地保存路径。
        :param expected_size: 期望的文件大小（用于验证），可为 None。
        :return: 成功返回 True，失败返回 False。
        """
        try:
            await self.semaphore.acquire()
            await self.pause_event.wait()

            chunk_size = 1 * 1024 * 1024  # 1MB
            async with self.client.stream("GET", url, timeout=15.0) as response:
                response.raise_for_status()
                content_length = response.headers.get("content-length")
                real_size = int(content_length) if content_length is not None else 0

                # 若处于字节模式且大小发生变化，更新 total_bytes
                if self.use_byte_progress:
                    if expected_size is None or real_size != expected_size:
                        if expected_size is None:
                            self.total_bytes += real_size
                        else:
                            self.total_bytes += real_size - expected_size

                path.parent.mkdir(parents=True, exist_ok=True)
                temp_path = path.with_suffix(path.suffix + ".tmp")
                with temp_path.open("wb") as f:
                    async for chunk in response.aiter_bytes(chunk_size):
                        await self.pause_event.wait()
                        await self.rate_limiter.acquire(len(chunk))
                        f.write(chunk)
                        self.bytes_downloaded_for_speed += len(chunk)
                        if self.use_byte_progress:
                            self.downloaded_bytes += len(chunk)
                            progress = min(self.downloaded_bytes, self.total_bytes) if self.total_bytes > 0 else 0
                            self._put_event("progress", progress, self.total_bytes)
                temp_path.replace(path)

            # 验证文件大小
            final_size = path.stat().st_size
            if 0 < real_size != final_size:
                path.unlink(missing_ok=True)
                return False

            self._mark_completed(url, path)
            if not self.use_byte_progress:
                self.downloaded_bytes += 1
                self._put_event("progress", self.downloaded_bytes, self.total_bytes)
            return True
        except Exception:
            return False
        finally:
            self.semaphore.release()

    # ---------- 主下载入口 ----------
    async def _download_file_once(self, url: str, path: Path, size_known: int | None) -> bool:
        """
        下载单个文件，若文件大于阈值且支持 Range，则启用智能分片下载；否则回退到流式下载。

        :param url: 文件 URL。
        :param path: 本地保存路径。
        :param size_known: 预检时已知的文件大小，可能为 None。
        :return: 下载成功返回 True，否则 False。
        """
        # 如果文件已存在且大小匹配，直接标记完成
        if size_known is not None and path.exists() and path.stat().st_size == size_known:
            self._mark_completed(url, path)
            if not self.use_byte_progress:
                self.downloaded_bytes += 1
                self._put_event("progress", self.downloaded_bytes, self.total_bytes)
            return True

        # 获取文件真实大小和 Range 支持情况
        file_size = size_known
        supports_range = False
        if file_size is None:
            # 发送 HEAD 请求获取大小和 Accept-Ranges
            try:
                head_resp = await self.client.head(url)
                head_resp.raise_for_status()
                content_length = head_resp.headers.get("content-length")
                if content_length:
                    file_size = int(content_length)
                accept_ranges = head_resp.headers.get("accept-ranges", "").lower()
                if accept_ranges == "bytes":
                    supports_range = True
            except Exception:
                # HEAD 失败，回退到普通下载
                pass

        # 判断是否启用分片
        threshold = int(self.chunk_threshold_mb * 1024 * 1024)
        if file_size is not None and file_size > threshold and supports_range:
            # ---------- 智能分片 ----------
            actual_chunk_size, num_chunks = self._calculate_chunk_params(file_size)
            chunk_paths: list[Path] = []
            tasks = []
            for i in range(num_chunks):
                start = i * actual_chunk_size
                end = min(start + actual_chunk_size - 1, file_size - 1)
                temp_path = path.with_suffix(f"{path.suffix}.part{i}.tmp")
                chunk_paths.append(temp_path)
                tasks.append(
                    self._download_chunk(url, start, end, temp_path)
                )

            # 执行分片下载，支持单个分片重试
            success = False
            for attempt in range(self.max_chunk_retries + 1):
                results = await asyncio.gather(*tasks, return_exceptions=True)
                failed_indices = []
                for idx, res in enumerate(results):
                    if isinstance(res, Exception) or not res:
                        failed_indices.append(idx)
                if not failed_indices:
                    success = True
                    break
                if attempt == self.max_chunk_retries:
                    break
                # 仅重试失败的分片
                for idx in failed_indices:
                    start = idx * actual_chunk_size
                    end = min(start + actual_chunk_size - 1, file_size - 1)
                    tasks[idx] = self._download_chunk(
                        url, start, end, chunk_paths[idx]  # type: ignore[assignment]
                    )

            if success:
                # 合并分片
                await self._merge_chunks(chunk_paths, path)
                self._mark_completed(url, path)
                if not self.use_byte_progress:
                    self.downloaded_bytes += 1
                    self._put_event("progress", self.downloaded_bytes, self.total_bytes)
                return True
            else:
                # 清理所有临时分片
                for p in chunk_paths:
                    p.unlink(missing_ok=True)
                return False

        else:
            # ---------- 普通流式下载 ----------
            return await self._download_stream(url, path, file_size)

    # ---------- 事件处理 ----------
    def _put_event(self, event_type: str, *args):
        """将事件放入异步队列（非阻塞）。"""
        asyncio.create_task(self.async_event_queue.put((event_type, args)))

    async def _event_collector(self):
        """将异步队列中的事件转移到同步队列，供回调线程消费。"""
        while True:
            item = await self.async_event_queue.get()
            if item is None:
                break
            self.sync_event_queue.put(item)

    def _dispatcher(self):
        """在独立线程中串行执行用户回调，保证线程安全。"""
        while not self._stop_dispatcher.is_set():
            try:
                item = self.sync_event_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            if item is None:
                break
            event_type, args = item
            if event_type == "progress":
                self.progress_callback(*args)
            elif event_type == "speed":
                self.speed_callback(*args)

    async def _speed_calculator(self):
        """每秒计算一次实时下载速度（基于独立字节计数器）。"""
        while True:
            await asyncio.sleep(1.0)
            now = time.monotonic()
            delta = now - self._last_speed_time
            if delta > 0:
                current = self.bytes_downloaded_for_speed
                speed = (current - self._last_downloaded) / delta / (1024 * 1024)
                self._last_downloaded = current
                self._last_speed_time = now
                self._put_event("speed", speed)

    async def _adaptive_concurrency(self):
        """
        不限速时，根据错误率动态调节并发数（AIMD 算法）。
        若存在失败条目，则降低并发；否则缓慢增加。
        """
        while True:
            await asyncio.sleep(2.0)
            if self.failed_entries:
                new_concurrency = max(10, int(self.concurrency * 0.8))
            else:
                new_concurrency = min(500, self.concurrency + 5)
            if new_concurrency != self.concurrency:
                self.concurrency = new_concurrency
                self.semaphore.change(new_concurrency)

    # ---------- 主运行 ----------
    async def run(self):
        """
        启动下载任务，直到所有文件下载完成或达到最大重试轮数。

        这是一个异步协程，需在事件循环中 await 调用。
        下载过程会自动处理并发控制、限速、暂停/恢复状态，并在结束后清理资源。

        :raises: 该方法会捕获内部异常并记录到失败列表，但不会向外抛出，
                 所有异常都通过回调或日志体现。若需要外部感知，请检查 self.failed_entries。
        """
        if self.loop is None:
            self.loop = asyncio.get_running_loop()

        self.client = httpx.AsyncClient(http2=True, timeout=httpx.Timeout(15, connect=5), headers=self.headers, follow_redirects=True)

        # 预检或跳过预检
        if self.skip_preflight:
            # 直接使用文件计数模式
            self.use_byte_progress = False
            self.total_files = len(self.original_downloads)
            self.total_bytes = self.total_files
            self.pending_entries = []
            for url, path in self.original_downloads:
                if not self._is_entry_completed(url, path) and not self._is_entry_failed(url, path):
                    self.pending_entries.append((url, path, None))
            self.downloaded_bytes = len(self.completed_entries)
        else:
            await self._preflight()

        # 若处于文件计数模式，从已完成状态恢复计数
        if not self.use_byte_progress:
            self.downloaded_bytes = len(self.completed_entries)

        # 构建本轮待下载列表（排除已完成）
        pending: list[tuple[str, Path, int | None]] = [
            (url, path, size) for url, path, size in self.pending_entries
            if not self._is_entry_completed(url, path)
        ]

        if not pending:
            return

        # 启动辅助协程和线程
        collector_task = asyncio.create_task(self._event_collector())
        speed_task = asyncio.create_task(self._speed_calculator())
        self._stop_dispatcher.clear()
        self._dispatcher_thread = threading.Thread(target=self._dispatcher, daemon=True)
        self._dispatcher_thread.start()

        adjuster_task = None
        if self.speed_limit_mb == 0:
            adjuster_task = asyncio.create_task(self._adaptive_concurrency())

        # 轮次重试
        round_num = 1
        while pending and round_num <= self.max_rounds:
            tasks = [asyncio.create_task(self._download_file_once(url, path, size))
                     for url, path, size in pending]
            results = await asyncio.gather(*tasks, return_exceptions=False)

            failed_this_round: list[tuple[str, Path, int | None]] = []
            for i, success in enumerate(results):
                if not success:
                    url, path, size = pending[i]
                    # 二次确认文件是否实际存在且完整
                    if path.exists() and (size is None or path.stat().st_size == size):
                        self._mark_completed(url, path)
                        if not self.use_byte_progress:
                            self.downloaded_bytes += 1
                            self._put_event("progress", self.downloaded_bytes, self.total_bytes)
                        continue
                    failed_this_round.append(pending[i])

            if not failed_this_round:
                pending = []
                break

            pending = failed_this_round
            round_num += 1
            if round_num <= self.max_rounds:
                wait_time = 2 ** (round_num - 1)  # 指数退避
                await asyncio.sleep(wait_time)

        # 剩余失败标记为永久失败
        if pending:
            for url, path, _ in pending:
                self._mark_failed(url, path)

        # ---------- 清理资源 ----------
        if adjuster_task:
            adjuster_task.cancel()
            try:
                await adjuster_task
            except:
                pass

        # 确保暂停事件已设置，防止任务阻塞
        self.pause_event.set()

        # 停止事件收集
        await self.async_event_queue.put(None)

        # 取消速度计算任务
        speed_task.cancel()
        try:
            await asyncio.gather(collector_task, speed_task, return_exceptions=True)
        except:
            pass

        # 停止回调分发线程
        self._stop_dispatcher.set()
        if self._dispatcher_thread:
            self._dispatcher_thread.join(timeout=2)

        # 关闭 HTTP 客户端
        await self.client.aclose()

        # 发送最终进度
        if self.use_byte_progress:
            progress = min(self.downloaded_bytes, self.total_bytes) if self.total_bytes > 0 else 0
            self._put_event("progress", progress, self.total_bytes)
        else:
            progress = min(self.downloaded_bytes, self.total_files)
            self._put_event("progress", progress, self.total_files)
        await asyncio.sleep(0.5)

    # ---------- 暂停/恢复 ----------
    def pause(self):
        """
        暂停所有正在进行的下载任务。

        调用后，正在下载的任务会在下一个检查点（每次写入块之前）阻塞在暂停事件上，
        不会继续下载新数据。该方法立即返回，不会等待任务实际暂停。
        """
        if self.pause_event.is_set():
            self.pause_event.clear()

    def resume(self):
        """
        恢复暂停的下载任务。

        调用后，所有被阻塞的任务将继续执行。
        注意：该操作不会重新调度已被取消的任务（本下载器采用事件阻塞，不取消任务）。
        """
        if not self.pause_event.is_set():
            self.pause_event.set()

    def stop(self):
        """
        完全停止下载器并关闭所有资源。

        调用后会先暂停下载，然后关闭 HTTP 客户端和辅助线程。
        该方法立即返回，但清理工作在后台完成。
        """
        self.pause()
        if self.loop and not self.loop.is_closed():
            asyncio.run_coroutine_threadsafe(self._safe_stop(), self.loop)

    async def _safe_stop(self):
        """实际执行停止清理。"""
        self.pause_event.set()  # 唤醒所有阻塞的任务
        if self.client:
            await self.client.aclose()
        self._stop_dispatcher.set()
        if self._dispatcher_thread:
            self._dispatcher_thread.join(timeout=1)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()