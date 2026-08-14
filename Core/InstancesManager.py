from typing import Callable
from pathlib import Path
from uuid import uuid4
import subprocess
import sys
import threading
import time

class InstancesManager:
    def __init__(self):
        self.instances: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._log_callback: Callable[[str, str], None] = lambda log, instance_id: print(f"[{instance_id}] {log}")
        self._exit_callback: Callable[[int, str], None] = lambda code, name: print(f"进程 {name} 退出，代码 {code}")

    # ---------- 回调设置 ----------
    def set_log_callback(self, callback: Callable[[str, str], None]) -> None:
        """
        设置 Log 的 Callback
        :param callback: 接受一个这样的函数 (log: str, instance_id: str) -> None
        :return: Nothing
        """
        self._log_callback = callback

    def set_exit_callback(self, callback: Callable[[int, str], None]) -> None:
        """
        设置 Exit Code 的 Callback
        :param callback: 接受一个这样的函数 (exit_code: int, instance_id: str) -> None
        :return: Nothing
        """
        self._exit_callback = callback

    # ---------- 内部流读取线程 ----------
    def _read_stream(
        self,
        stream,
        callback: Callable[[str, str], None],
        proc: subprocess.Popen,
        instance_id: str,
        instance_name: str,
        exit_callback: Callable[[int, str], None]
    ) -> None:
        """
        读取一个输出流（stdout 或 stderr），逐行回调。
        当流关闭时，负责触发一次退出回调（仅第一次触发）。
        """
        try:
            for line in iter(stream.readline, ""):
                if line:
                    callback(line.rstrip("\n"), instance_id)
        except (OSError, ValueError):
            # 管道意外关闭，线程安全退出
            pass
        finally:
            stream.close()

            # ========== 修复点1：使用锁 + 状态标志防止多次触发退出回调 ==========
            with self._lock:
                inst = self.instances.get(instance_id)
                if inst is None:
                    return
                # 如果已经标记为已退出，则忽略（另一个线程已处理）
                if inst.get("_exited", False):
                    return
                # 标记为已退出，防止重复
                inst.update({"_exited": True})

            # 在锁外等待进程结束（避免死锁）
            # 注意：如果 stdout 线程先进入这里，它会阻塞直到进程退出。
            # 而 stderr 线程会在同一个 finally 块中检查 _exited 标记，不会重复触发。
            return_code = proc.wait()

            # 从实例表中移除
            with self._lock:
                self.instances.pop(instance_id, None)

            # 触发退出回调（带实例名称，便于识别）
            exit_callback(return_code, instance_name)

    # ---------- 创建实例 ----------
    def create_instance(
        self,
        instance_name: str,
        instance_type: str,
        args: str | list[str],
        cwd: str | Path | None = None,
        new_session: bool = True,
        only_stdout: bool = False,
        std_in: bool = False,
        log_callback: Callable[[str, str], None] | None = None,
        exit_callback: Callable[[int, str], None] | None = None,
        block_thread: bool = False
    ) -> str:
        """
        创建一个新的实例(子进程), 并启动日志读取线程
        :param instance_name: 实例名称
        :param instance_type: 实例类型
        :param args: 指令
        :param cwd: 工作路径
        :param new_session: 是否以新会话启动(关闭父进程子进程不退出)
        :param only_stdout: 是否将 STDOUT 和 STDERR 合并为 STDOUT
        :param std_in: 是否开启 STDIN 管道
        :param log_callback: 接受一个这样的函数 (log: str, instance_id: str) -> None
        :param exit_callback: 接受一个这样的函数 (exit_code: int, instance_id: str) -> None
        :param block_thread: 是否阻塞调用线程直到子进程退出
        :return: 实例 ID(uuid4.hex)
        """
        proc = subprocess.Popen(
            args,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE if not only_stdout else subprocess.STDOUT,
            stdin=subprocess.PIPE if std_in else None,
            bufsize=1,
            start_new_session=new_session,
            text=True,
            encoding="utf-8",
            errors="ignore"
        )

        instance_id = uuid4().hex
        callback = log_callback or self._log_callback
        exit_cb = exit_callback or self._exit_callback

        # 必须先登记实例，再启动读取线程。Java 在参数或运行环境不兼容时可能在数毫秒内
        # 退出；若线程先读到 EOF，旧实现会因为实例尚未登记而丢失唯一的退出回调。
        with self._lock:
            self.instances[instance_id] = {
                "Name": instance_name,
                "ID": instance_id,
                "Type": instance_type,
                "StdIn": std_in,
                "Instance": proc,
                "Threads": [],
                "ExitCallback": exit_cb,
                "_exited": False,
            }

        # ---------- 明确区分 stdout 和 stderr 线程 ----------
        # stdout 线程负责触发退出回调（因为 stdout 通常最后关闭）
        t_out = threading.Thread(
            target=self._read_stream,
            args=(proc.stdout, callback, proc, instance_id, instance_name, exit_cb),
            daemon=True
        )
        t_err = None
        if not only_stdout and proc.stderr:
            t_err = threading.Thread(
                target=self._read_stream,
                # 注意：stderr 线程也传了相同的参数，但 _read_stream 内部会通过 _exited 标志防止重复触发
                args=(proc.stderr, callback, proc, instance_id, instance_name, exit_cb),
                daemon=True
            )
        with self._lock:
            instance = self.instances.get(instance_id)
            if instance is not None:
                instance["Threads"] = [t_out, t_err] if t_err else [t_out]

        t_out.start()
        if t_err is not None:
            t_err.start()

        if block_thread:
            proc.wait()  # 等待进程退出

        return instance_id

    # ---------- 标准输入 ----------
    def send_stdin(self, instance_id: str, data: str) -> None:
        """
        向实例(子进程)发送数据
        :param instance_id: 实例 ID
        :param data: 数据
        :return: None
        """
        if instance_id not in self.instances:
            return
        inst = self.instances[instance_id]
        if not inst["StdIn"]:
            return
        proc: subprocess.Popen = inst["Instance"]
        if proc.stdin and proc.poll() is None:
            try:
                proc.stdin.write(data)
                proc.stdin.flush()
            except (BrokenPipeError, OSError):
                pass

    # ---------- 停止实例（重点修复） ----------
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
            if proc.poll() is not None:
                return True

        # 在锁外执行终止操作，避免死锁
        if force:
            proc.kill()
        else:
            proc.terminate()

        # ---------- 修复点3：提供等待机制，确保进程真正结束 ----------
        if wait_timeout is not None:
            try:
                proc.wait(timeout=wait_timeout)
                return True
            except subprocess.TimeoutExpired:
                # 超时后强制杀死
                proc.kill()
                proc.wait()
                return False
        return True

    @staticmethod
    def _notify_process_exit(proc: subprocess.Popen) -> bool:
        """
        通知子进程自行退出。

        Windows 的 ``Popen.terminate()`` 会直接调用 ``TerminateProcess``，无法让
        Minecraft 保存并执行正常关闭流程。因此 Windows 向属于 Java 进程的顶层
        窗口投递 ``WM_CLOSE``；其他平台继续使用可由进程处理的终止信号。

        :param proc: 需要通知的子进程
        :return: 是否成功发送退出通知
        """
        if sys.platform != "win32":
            proc.terminate()
            return True

        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        enum_windows_callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user32.EnumWindows.argtypes = [enum_windows_callback, wintypes.LPARAM]
        user32.EnumWindows.restype = wintypes.BOOL
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.PostMessageW.restype = wintypes.BOOL
        notified = False

        @enum_windows_callback
        def notify_window(window_handle, _parameter):
            nonlocal notified
            process_id = wintypes.DWORD()
            user32.GetWindowThreadProcessId(window_handle, ctypes.byref(process_id))
            if process_id.value == proc.pid and user32.PostMessageW(window_handle, 0x0010, 0, 0):
                notified = True
            return True

        user32.EnumWindows(notify_window, 0)
        return notified

    def request_instance_exit(self, instance_id: str, wait_timeout: float | int = 3.0) -> bool:
        """
        请求指定实例正常退出，超时后才强制结束。

        :param instance_id: 实例 ID
        :param wait_timeout: 等待实例响应退出通知的秒数
        :return: True 表示实例自行退出，False 表示等待超时后被强制结束
        """
        with self._lock:
            inst = self.instances.get(instance_id)
            if not inst:
                return True
            proc: subprocess.Popen = inst["Instance"]
            if proc.poll() is not None:
                return True

        self._notify_process_exit(proc)
        try:
            proc.wait(timeout=wait_timeout)
            return True
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            return False

    def get_instances_info(self) -> list:
        """
        获取实例信息列表
        :return: 实例信息列表
        """
        with self._lock:
            return list(self.instances.values())

    # ---------- 修复点4：优雅关闭所有实例 ----------
    def shutdown_all(self, force: bool = False, wait_timeout: float = 3.0) -> None:
        """
        终止所有正在运行的实例。
        :param force: True 使用 kill()，False 使用 terminate()
        :param wait_timeout: 每个实例等待结束的超时时间
        :return: None
        """
        # 先复制一份 ID 列表，避免遍历时修改 dict
        with self._lock:
            ids = list(self.instances.keys())

        for pid in ids:
            self.stop_instance(pid, force=force, wait_timeout=wait_timeout)

        # ---------- 修复点5：等待所有读取线程自然结束（最多再等1秒） ----------
        # 由于线程是 daemon=True，主程序退出时会被强制终止，
        # 但在正常关闭流程中，我们等待一下确保日志被刷新。
        time.sleep(0.1)
        with self._lock:
            # 清理残留的实例记录（理论上 stop_instance 已移除）
            for pid in list(self.instances.keys()):
                inst = self.instances.get(pid)
                if inst and inst["Instance"].poll() is not None:
                    self.instances.pop(pid, None)
