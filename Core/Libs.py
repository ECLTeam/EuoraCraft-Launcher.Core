# ============================================================
# EuoraCraft Launcher Core
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher.Core
#
# 文件作用：Core 通用辅助函数，含路径换算、离线 UUID、校验、解压与原子写入。
#
# 公开接口：
#   - atomic_write_bytes(path, data) -> None — 通过同目录临时文件原子替换文件内容。
#   - atomic_write_text(path, data, encoding=…) -> None — 以原子替换方式写入文本文件。
#   - class AtomicWritePolicy — 原子替换时的 Windows 短暂占用重试策略。
#   - find_version / get_file_sha1 / name_to_path / name_to_uuid / unzip / parse_datetime 等既有辅助函数。
# ============================================================

from datetime import datetime, timezone, timedelta
from pathlib import Path
from time import sleep
from uuid import UUID, uuid4
import hashlib
import zipfile
import json
import os
import re


def replace_last(text: str, old: str, new: str) -> str:
    """
    替换字符串最后一个匹配项
    :param text: 字符串
    :param old: 需要被替换的内容
    :param new: 替换的内容
    :return: 修改后的字符串
    """
    return new.join(text.rsplit(old, 1))


def name_to_path(name: str) -> str:
    """
    这其实不是一个公开的函数，为了调用方便罢了
    :param name: Maven 的 name 键值
    :return: 正确拼接的路径
    """
    at_index = name.find("@")
    if at_index != -1:
        suffix = name[at_index + 1:]
        name = name[0:at_index]
    else:
        suffix = "jar"
    parts = name.split(":")
    if len(parts) == 4:
        return f"{parts[0].replace('.', '/')}/{parts[1]}/{parts[2]}/{parts[1]}-{parts[2]}-{parts[3]}.{suffix}"
    elif len(parts) == 3:
        return f"{parts[0].replace('.', '/')}/{parts[1]}/{parts[2]}/{parts[1]}-{parts[2]}.{suffix}"
    else:
        return ""


def name_to_uuid(name: str) -> UUID:
    """
    Minecraft 离线玩家 UUID(UUID3) 计算
    :param name: 玩家昵称
    :return: UUID(UUID3) 对象
    """
    return UUID(bytes=hashlib.md5(f"OfflinePlayer:{name}".encode("utf-8")).digest()[:16], version=3)


def is_uuid3(uuid_string: str) -> bool:
    """
    检测一个字符串是否为 UUID3
    :param uuid_string: UUID 字符串
    :return: bool 值
    """
    try:
        return UUID(uuid_string, version=3).version == 3
    except ValueError:
        return False


def unzip(zip_path: str | Path, unzip_path: str | Path) -> bool:
    """
    解压文件
    :param zip_path: 压缩包路径
    :param unzip_path: 目标路径
    :return: bool 值, 是否完成解压
    """
    try:
        with zipfile.ZipFile(zip_path) as zip_object:
            for file in zip_object.namelist():
                zip_object.extract(file, unzip_path)
        return True
    except (zipfile.BadZipFile, FileNotFoundError):
        return False


def get_file_sha1(file_path: str | Path) -> str:
    """
    获取文件 Sha1
    :param file_path: 文件路径
    :return: Sha1 字符串
    """
    sha1 = hashlib.sha1()
    if os.path.isfile(file_path):
        with open(file_path, "rb") as open_file:
            for file_part in iter(lambda: open_file.read(8192), b""):
                sha1.update(file_part)
    return sha1.hexdigest()


def find_version(version_json: dict, game_path: Path | str, version_name: str | None = None) -> tuple[dict, Path] | None:
    """
    查找 Meta Json 的 inheritsFrom 键值对应游戏版本
    :param version_json: Meta Json 内容
    :param game_path: .minecraft 路径
    :param version_name: 这是为了适配版本合并
    :return: None 为没找到, 或对应版本 (MetaJson内容, 路径)
    """
    game_path = Path(game_path)
    if "inheritsFrom" in version_json:  # 若有Mod加载器则寻找原版游戏
        inherits_from = version_json["inheritsFrom"]
        if version_name:
            json_path = game_path / "versions" / version_name / f"{inherits_from}.json"
            if json_path.is_file():
                return json.loads(json_path.read_text("utf-8")), json_path.parent
        for version_path in (game_path / "versions").iterdir():  # 通过版本Json内的id键查找是否为对应的游戏版本, 而不是根据Json的名字判断
            if not version_path.is_dir(): continue
            game_json_path = version_path / f"{version_path.name}.json"
            if not game_json_path.is_file(): continue
            game_json = json.loads(game_json_path.read_text("utf-8"))
            if game_json["id"] != inherits_from: continue
            return game_json, version_path
        version_path = game_path / "versions" / inherits_from
        if (version_path / f"{inherits_from}.json").is_file():  # 如果没找到则尝试直接找inheritsFrom对应的版本
            return json.loads((version_path / f"{inherits_from}.json").read_text("utf-8")), version_path
        return None
    return None


def set_minecraft_lang(game_path: Path | str, version_name: str, lang: str):
    """
    设置 Minecraft 的语言
    :param game_path: .minecraft 路径
    :param version_name: 版本名称
    :param lang: 语言, 如: zh_CN
    :return:
    """
    options_contents = set_lang = f"lang:{lang}"
    options_path = Path(game_path) / "versions" / version_name / "options.txt"
    if options_path.is_file():
        options_contents = options_path.read_text("utf-8")
        options_contents = re.sub(r"^lang:\S+$", set_lang, options_contents, flags=re.MULTILINE)
    options_path.write_text(options_contents, "utf-8")


def parse_datetime(time_str: str):  # 前端不建议用
    """
    解析时间字符串，提取日期、时间、时区，并转换为UTC+8(偏移8h)
    :param time_str: ISO格式时间字符串，如 "2025-12-16T12:42:29+00:00"(Minecraft 26.1-snapshot-1)
    :return: dict[str, dict[str, datetime | date | time | tzinfo | None | str | timedelta]]
    """
    # 解析时间字符串
    original_dt = datetime.fromisoformat(time_str)
    # 转换为UTC+8时间
    converted_dt = original_dt.astimezone(timezone(timedelta(hours=8)))
    return {
        "Original": {
            "Datetime": original_dt,
            "Date": original_dt.date(),  # 日期
            "Time": original_dt.time(),  # 时间
            "Timezone": original_dt.tzinfo,  # 时区
            "Iso": original_dt.isoformat(),  # ISO格式时间
            "Offset": original_dt.utcoffset(),  # 偏移
        },
        "Converted": {
            "Datetime": converted_dt,
            "Date": converted_dt.date(),
            "Time": converted_dt.time(),
            "Timezone": converted_dt.tzinfo,
            "Iso": converted_dt.isoformat(),
            "Offset": converted_dt.utcoffset(),
        }
    }


class AtomicWritePolicy:
    """
    原子替换文件时的 Windows 短暂占用重试策略。

    延迟序列用于兼容杀毒软件和索引器短暂占用刚写入的文件。
    """

    windows_replace_retry_delays = (0.02, 0.05, 0.1, 0.2)


def atomic_write_bytes(path: str | Path, data: bytes) -> None:
    """
    通过同目录临时文件实现原子替换文件内容。

    临时文件与目标同目录，替换失败时保留原文件；重试耗尽后抛出异常，
    由调用方决定回滚语义。

    :param path: 需要写入的文件路径
    :param data: 待持久化的字节数据
    """
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_bytes(data)
        for delay in (*AtomicWritePolicy.windows_replace_retry_delays, None):
            try:
                temporary.replace(destination)
                break
            except PermissionError:
                if delay is None:
                    raise
                # Windows 上杀毒软件、索引器或另一条刚结束的替换操作可能短暂占用目标。
                # 临时文件仍在同一目录，重试不会破坏原子替换语义。
                sleep(delay)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_text(path: str | Path, data: str, encoding: str = "utf-8") -> None:
    """
    以原子替换方式写入文本文件。

    :param path: 需要写入的文件路径
    :param data: 待持久化的文本内容
    :param encoding: 文本编码，默认 UTF-8
    """
    atomic_write_bytes(path, data.encode(encoding))
