"""执行有界 Java 属性探测，并等待本次进程和输出读取实际结束。"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable, Mapping
from io import BufferedReader
from pathlib import Path
from threading import Event, Thread
from time import monotonic, sleep


class JavaProbeError(RuntimeError):
    """区分超时、输出超限及无法完成探测，不包含执行路径或原始输出。"""

    def __init__(self, code: str) -> None:
        super().__init__("Java 属性探测未完成")
        self.code = code


def probe_output(
    executable: Path,
    deadline: float,
    max_output_bytes: int,
    environment: Mapping[str, str] | None,
    check: Callable[[], None] | None,
) -> str | None:
    """按固定参数启动一个探测；检查失败后仍确认进程和读取线程实际退出。"""
    if check is not None:
        check()
    deadline = min(deadline, monotonic() + 10)
    if deadline <= monotonic():
        raise JavaProbeError("deadline")
    output = bytearray()
    finished = Event()
    exceeded = Event()
    read_failed = Event()
    process = subprocess.Popen(
        [str(executable), "-XshowSettings:properties", "-version"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        shell=False,
        env=environment,
        **({"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}),
    )
    assert process.stdout is not None

    reader = Thread(
        target=_read_output,
        args=(process.stdout, max_output_bytes, output, finished, exceeded, read_failed),
        name="java_probe_output",
        daemon=True,
    )
    try:
        reader.start()
        while True:
            if check is not None:
                check()
            if exceeded.is_set():
                raise JavaProbeError("output_limit")
            if monotonic() >= deadline:
                raise JavaProbeError("deadline")
            if finished.is_set() and process.poll() is not None:
                break
            sleep(0.02)
        if read_failed.is_set():
            raise JavaProbeError("read_failed")
        return output.decode("utf-8", errors="replace") if process.returncode == 0 else None
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        if reader.ident is not None:
            reader.join()
        else:
            process.stdout.close()


def _read_output(
    stream: BufferedReader,
    max_output_bytes: int,
    output: bytearray,
    finished: Event,
    exceeded: Event,
    read_failed: Event,
) -> None:
    """在读取期间限制实际保留的字节，达到上限即关闭管道并通知进程所有者。"""
    try:
        while chunk := stream.read1(4096):
            if len(output) + len(chunk) > max_output_bytes:
                exceeded.set()
                break
            output.extend(chunk)
    except OSError:
        read_failed.set()
    finally:
        stream.close()
        finished.set()
