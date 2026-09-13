import subprocess
import sys
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from uuid import uuid4

import psutil

def _windows_priority_classes() -> dict[str, int]:
    """仅在 Windows 读取 psutil 的平台专属优先级常量。"""
    if sys.platform != "win32":
        return {}
    return {
        "idle": psutil.IDLE_PRIORITY_CLASS,
        "below_normal": psutil.BELOW_NORMAL_PRIORITY_CLASS,
        "normal": psutil.NORMAL_PRIORITY_CLASS,
        "above_normal": psutil.ABOVE_NORMAL_PRIORITY_CLASS,
        "high": psutil.HIGH_PRIORITY_CLASS,
    }


# Windows 进程优先级名称到 psutil 优先级类的映射。
_WIN_PRIORITY_CLASSES = _windows_priority_classes()
# POSIX 优先级名称到 nice 值的映射；数值越小优先级越高。
_POSIX_PRIORITY_NICE: dict[str, int] = {
    "idle": 19,
    "below_normal": 10,
    "normal": 0,
    "above_normal": -5,
    "high": -10,
}

class InstancesManager:
    def __init__(
        self,
        log_callback: Callable[[str, str], None] | None = None,
        exit_callback: Callable[[int, str], None] | None = None
    ):
        """
        :param log_callback: 无论如何都能收集到所有实例的 Log
        :param exit_callback: 无论如何都能收集到所有实例的 退出码
        """
        self.instances: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._log_callback = log_callback or (lambda log, instance_id: print(f"[{instance_id}] {log}"))
        self._exit_callback = exit_callback or (lambda code, name: print(f"进程 {name} 退出，代码 {code}"))

    @staticmethod
    def _noop(*_): pass

    # ---------- 回调设置 ----------
    def set_log_callback(self, callback: Callable[[str, str], None]) -> None:
        """重新设置 Log 回调"""
        self._log_callback = callback

    def set_exit_callback(self, callback: Callable[[int, str], None]) -> None:
        """重新设置 退出码 回调"""
        self._exit_callback = callback

    # ---------- 内部流读取线程 ----------
    def _read_stream(
        self,
        stream,
        callback: Callable[[str, str], None],
        proc: subprocess.Popen,
        instance_id: str,
        exit_callback: Callable[[int, str], None]
    ) -> None:
        """读取 stdout 流（stderr 已合并），逐行回调，进程退出时触发退出回调"""
        try:
            for line in iter(stream.readline, ""):
                if line:
                    log = line.rstrip("\n")
                    callback(log, instance_id)
                    self._log_callback(log, instance_id)
        finally:
            stream.close()
            return_code = proc.wait()   # 等待进程真正结束
            with self._lock:
                self.instances.pop(instance_id, None)
            exit_callback(return_code, instance_id)
            self._exit_callback(return_code, instance_id)

    # ---------- 创建实例 ----------
    def create_instance(
        self,
        instance_name: str,
        instance_type: str,
        args: str | list[str],
        cwd: str | Path | None = None,
        new_session: bool = False,
        std_in: bool = False,
        log_callback: Callable[[str, str], None] | None = None,
        exit_callback: Callable[[int, str], None] | None = None,
        block_thread: bool = False,
        env: dict[str, str] | None = None,
        priority: str = "normal"
    ) -> tuple[str, subprocess.Popen]:
        """
        创建一个新的子进程实例，所有输出（stdout+stderr）合并到 stdout
        :param instance_name: 实例名称
        :param instance_type: 实例类型
        :param args: 指令
        :param cwd: 工作路径
        :param new_session: 是否以新会话启动（父进程关闭子进程不退出）
        :param std_in: 是否开启 STDIN 管道
        :param log_callback: 回调 (log: str, instance_id: str) -> None
        :param exit_callback: 回调 (exit_code: int, instance_id: str) -> None
        :param block_thread: 是否阻塞调用线程直到子进程退出
        :param env: 附加环境变量，None 时继承父进程环境
        :param priority: 进程优先级: idle / below_normal / normal / above_normal / high
        :return: (实例ID(uuid4.hex), subprocess.Popen)
        """
        log_callback = log_callback or self._noop
        exit_callback = exit_callback or self._noop

        instance_id = uuid4().hex

        proc = subprocess.Popen(
            args,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,          # 合并 stderr 到 stdout
            stdin=subprocess.PIPE if std_in else None,
            bufsize=1,
            start_new_session=new_session,
            text=True,
            encoding="utf-8",
            errors="ignore",
            env=env
        )
        self._apply_priority(proc, priority)

        # 只启动一个 stdout 读取线程
        t_out = threading.Thread(
            target=self._read_stream,
            args=(proc.stdout, log_callback, proc, instance_id, exit_callback),
            daemon=True
        )
        t_out.start()

        with self._lock:
            self.instances[instance_id] = {
                "Name": instance_name,
                "ID": instance_id,
                "Type": instance_type,
                "StdIn": std_in,
                "Instance": proc,
                "Threads": t_out
            }

        if block_thread:
            proc.wait()

        return instance_id, proc

    @staticmethod
    def _apply_priority(proc: subprocess.Popen, priority: str) -> None:
        # 设置子进程优先级；越权或平台不支持时静默忽略，避免启动失败。
        is_windows = sys.platform == "win32"
        # Windows 用优先级类；POSIX 用 nice 值。
        target = (
            _WIN_PRIORITY_CLASSES.get(priority)
            if is_windows
            else _POSIX_PRIORITY_NICE.get(priority)
        )
        normal_priority = _WIN_PRIORITY_CLASSES.get("normal") if is_windows else _POSIX_PRIORITY_NICE["normal"]
        if target is None or target == normal_priority:
            return
        with suppress(psutil.Error, OSError, ValueError):
            psutil.Process(proc.pid).nice(target)

    # ---------- 标准输入 ----------
    def send_stdin(self, instance_id: str, data: str) -> bool:
        """
        向指定实例发送数据
        :param instance_id: 实例 ID
        :param data: 数据(因为指定了 `text=True` 所以是 str 类型)
        :return: 发送成功返回 True
        """
        if instance_id not in self.instances:
            return False
        inst = self.instances[instance_id]
        if not inst["StdIn"]:
            return False
        proc: subprocess.Popen = inst["Instance"]
        if proc.stdin and proc.poll() is None:
            try:
                proc.stdin.write(data)
                proc.stdin.flush()
                return True
            except (BrokenPipeError, OSError):
                pass
        return False

    # ---------- 停止实例 ----------
    def stop_instance(self, instance_id: str, force: bool = False, wait_timeout: float | int | None = None) -> bool:
        """
        停止指定实例
        :param instance_id: 实例 ID
        :param force: True 使用 kill()，False 使用 terminate()
        :param wait_timeout: 等待进程结束的超时时间(秒)，None 表示不等待
        :return: 进程是否已结束
        """
        with self._lock:
            inst = self.instances.get(instance_id)
            if not inst:
                return True
            proc: subprocess.Popen = inst["Instance"]
            if proc.poll():
                return True

        # 在锁外执行终止
        if force:
            proc.kill()
        else:
            proc.terminate()

        if wait_timeout:
            try:
                proc.wait(timeout=wait_timeout)
                return True
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                return False
        return True

    def get_instances_info(self) -> list:
        """获取全部实例的信息"""
        with self._lock:
            return list(self.instances.values())

    # ---------- 优雅关闭所有实例 ----------
    def shutdown_all(self, force: bool = False, wait_timeout: float = 3.0) -> None:
        """
        停止指定实例
        :param force: True 使用 kill()，False 使用 terminate()
        :param wait_timeout: 等待进程结束的超时时间(秒)，None 表示不等待
        :return: None
        """
        with self._lock:
            ids = list(self.instances.keys())

        for pid in ids:
            self.stop_instance(pid, force=force, wait_timeout=wait_timeout)

        # 短暂等待确保读取线程结束（daemon 线程会在主程序退出时自动终止，但这里预留刷新时间）
        time.sleep(0.1)
        with self._lock:
            for pid in list(self.instances.keys()):
                inst = self.instances.get(pid)
                if inst and inst["Instance"].poll() is not None:
                    self.instances.pop(pid, None)
