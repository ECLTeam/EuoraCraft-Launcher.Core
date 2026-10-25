from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class InstallEvent:
    """一次安装调用产生的阶段事实，不保存共享任务状态。"""

    stage: str
    status: Literal["running", "completed", "skipped"] = "running"
    action: str = "working"
    unit: Literal["bytes", "files", "items", "unknown"] = "unknown"
    completed: int = 0
    total: int | None = None
    completed_files: int = 0
    total_files: int | None = None
    transferred_bytes: int = 0
    speed: int = 0


@dataclass(frozen=True, slots=True)
class InstallObserver:
    """由调用方拥有的进度与取消检查；不得安装到缓存 Core 实例上。"""

    callback: Callable[[InstallEvent], None] = lambda event: None
    check_cancelled: Callable[[], None] = lambda: None

    def emit(self, stage: str, **details) -> None:
        self.check_cancelled()
        self.callback(InstallEvent(stage, **details))
