from typing import Callable
from pathlib import Path
from uuid import uuid4
import subprocess
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
        self._log_callback = callback

    def set_exit_callback(self, callback: Callable[[int, str], None]) -> None:
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
        """读取 stdout 流（stderr 已合并），逐行回调，进程退出时触发退出回调。"""
        try:
            for line in iter(stream.readline, ""):
                if line:
                    callback(line.rstrip("\n"), instance_id)
        except (OSError, ValueError):
            pass
        finally:
            stream.close()
            return_code = proc.wait()   # 等待进程真正结束
            with self._lock:
                self.instances.pop(instance_id, None)
            exit_callback(return_code, instance_name)

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
        block_thread: bool = False
    ) -> str:
        """
        创建一个新的子进程实例，所有输出（stdout+stderr）合并到 stdout。
        :param instance_name: 实例名称
        :param instance_type: 实例类型
        :param args: 指令
        :param cwd: 工作路径
        :param new_session: 是否以新会话启动（父进程关闭子进程不退出）
        :param std_in: 是否开启 STDIN 管道
        :param log_callback: 回调 (log: str, instance_id: str) -> None
        :param exit_callback: 回调 (exit_code: int, instance_id: str) -> None
        :param block_thread: 是否阻塞调用线程直到子进程退出
        :return: 实例 ID (uuid4.hex)
        """
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
            errors="ignore"
        )

        callback = log_callback or self._log_callback
        exit_cb = exit_callback or self._exit_callback
        instance_id = uuid4().hex

        # 只启动一个 stdout 读取线程
        t_out = threading.Thread(
            target=self._read_stream,
            args=(proc.stdout, callback, proc, instance_id, instance_name, exit_cb),
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
                "Threads": [t_out],
            }

        if block_thread:
            proc.wait()

        return instance_id

    # ---------- 标准输入 ----------
    def send_stdin(self, instance_id: str, data: str) -> None:
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
            if proc.poll() is not None:
                return True

        # 在锁外执行终止
        if force:
            proc.kill()
        else:
            proc.terminate()

        if wait_timeout is not None:
            try:
                proc.wait(timeout=wait_timeout)
                return True
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                return False
        return True

    def get_instances_info(self) -> list:
        with self._lock:
            return list(self.instances.values())

    # ---------- 优雅关闭所有实例 ----------
    def shutdown_all(self, force: bool = False, wait_timeout: float = 3.0) -> None:
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