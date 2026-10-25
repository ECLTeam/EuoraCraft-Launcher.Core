# EuoraCraft-Launcher.Core

EuoraCraft-Launcher启动器的核心，包含实例的安装下载，启动，账户管理等核心功能

## 安装阶段与下载进度

安装调用可通过公开入口的 `InstallObserver` 传入 `InstallEvent` 回调与取消检查。
`GetGames.build_*_download_list`、`FilesChecker.check_files`、安装器和下载器会沿本次调用传递 observer，
不会修改缓存 Core 对象的回调。观察者未提供时仍可独立使用 Core。

事件包含阶段、状态、动作、进度单位和可信总量，以及完成文件数、实际传输量和速度。
总量未知时为 `None`；实际传输量包含失败重试消耗，不用于计算成功字节百分比。
下载预检与重试会单独报告动作，安装处理器按实际完成数量报告；处理器非零退出码会明确失败。
最终安装状态由上层协调器在执行方退出后确认，取消检查抛出的异常会保留到上层处理。

```python
from ECL.game import InstallObserver

observer = InstallObserver(callback=on_install_event, check_cancelled=check_cancelled)
downloads = games.build_minecraft_download_list("1.20.1", "Example", observer=observer)
```

## 鸣谢
- [`xphost008`](https://github.com/xphost008) - 给该核心作者 [`AEBC08`](https://github.com/AEBC08) 提供了很多帮助
- [`TheMyceliumOfAntan`](https://github.com/TheMyceliumOfAntan) - 给该核心作者 [`AEBC08`](https://github.com/AEBC08) 提供了很多帮助

### 本项目学习了以下项目
- [HMCL-dev/HMCL](https://github.com/HMCL-dev/HMCL) - 学习了 Java 扫描部分

~~*"要想看得更远，得先站在巨人的肩膀上"*~~
