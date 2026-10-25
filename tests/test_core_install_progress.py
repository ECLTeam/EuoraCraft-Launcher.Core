from __future__ import annotations

import asyncio
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import anyio
import httpx
import pytest
from ECL.game import LoaderInstaller
from ECL.game.Core.Downloader import Downloader
from ECL.game.Core.InstallProgress import InstallObserver
from ECL.game.Core.NetLibs import ApiUrlConfig, BaseApiClient


def test_direct_client_reports_transfer_and_preserves_target_on_failure(tmp_path: Path):
    events = []
    client = BaseApiClient(ApiUrlConfig())
    client._client.close()
    client._client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"x" * 200000)))
    target = tmp_path / "client.jar"
    try:
        client.download_client_jar("hash", target, observer=InstallObserver(events.append))
        assert target.stat().st_size == 200000
        assert any(0 < event.completed < 200000 for event in events)
        assert events[-1].completed_files == 1
        client._client.close()
        client.max_retries = 1
        client._client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(500)))
        with pytest.raises(httpx.HTTPStatusError):
            client.download_client_jar("hash", target)
        assert target.stat().st_size == 200000
        assert not list(tmp_path.glob("*.tmp"))
    finally:
        client.close()


async def test_unknown_size_reports_transferred_bytes_and_final_callback(monkeypatch, tmp_path: Path):
    events, callbacks = [], []
    original = httpx.AsyncClient

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(5):
                await asyncio.sleep(0.06)
                yield b"x" * 65536

    def handler(request):
        if request.method == "HEAD":
            return httpx.Response(200)
        return httpx.Response(200, stream=Stream())

    monkeypatch.delenv("ECL_DOWNLOAD_PROXY", raising=False)
    monkeypatch.setattr("ECL.game.Core.Downloader.httpx.AsyncClient",
                        lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    downloader = Downloader([("http://example.test/file", tmp_path / "file")], max_rounds=1,
                            observer=InstallObserver(events.append),
                            progress_callback=lambda done, total: callbacks.append((done, total)))
    await downloader.run()
    assert any(event.transferred_bytes > 0 and event.completed_files == 0 for event in events)
    assert events[-1].completed_files == 1
    assert events[-1].transferred_bytes == 5 * 65536
    assert callbacks[-1] == (1, 1)
    assert downloader.client.is_closed


async def test_preflight_is_bounded_and_reports_checks(monkeypatch, tmp_path: Path):
    active, peak = 0, 0
    events = []
    original = httpx.AsyncClient

    async def handler(request):
        nonlocal active, peak
        if request.method == "HEAD":
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1
        return httpx.Response(200, content=b"file")

    monkeypatch.delenv("ECL_DOWNLOAD_PROXY", raising=False)
    monkeypatch.setattr("ECL.game.Core.Downloader.httpx.AsyncClient",
                        lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    downloader = Downloader([(f"http://example.test/{i}", tmp_path / str(i)) for i in range(12)],
                            observer=InstallObserver(events.append))
    downloader.concurrency = 3
    await downloader.run()
    assert peak <= 3
    checks = [event for event in events if event.action == "preflight" and event.unit == "items"]
    assert [event.completed for event in checks] == [3, 6, 9, 12]


async def test_transport_disconnect_retries_without_inflating_completed_bytes(monkeypatch, tmp_path: Path):
    events, attempts = [], 0
    original = httpx.AsyncClient

    class Interrupted(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"x" * 65536
            raise anyio.EndOfStream

    def handler(request):
        nonlocal attempts
        if request.method == "HEAD":
            return httpx.Response(200, headers={"content-length": "131072"})
        attempts += 1
        if attempts == 1:
            return httpx.Response(200, headers={"content-length": "131072"}, stream=Interrupted())
        return httpx.Response(200, content=b"x" * 131072)

    monkeypatch.delenv("ECL_DOWNLOAD_PROXY", raising=False)
    monkeypatch.setattr("ECL.game.Core.Downloader.httpx.AsyncClient",
                        lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    downloader = Downloader([("http://example.test/file", tmp_path / "file")],
                            observer=InstallObserver(events.append), max_rounds=2)
    await downloader.run()
    assert attempts == 2
    assert downloader.downloaded_bytes == 131072
    assert downloader.bytes_downloaded_for_speed == 196608
    assert any(event.action == "retrying" for event in events)
    assert events[-1].completed_files == 1
    assert (tmp_path / "file").stat().st_size == 131072


@pytest.mark.parametrize("platform_name", ["Windows", "Linux"])
@pytest.mark.parametrize("exit_code", [0, 1])
def test_processor_reports_items_checks_exit_and_adapts_log_callback(tmp_path, monkeypatch, platform_name, exit_code):
    monkeypatch.setattr("ECL.game.Core.LoaderInstaller.platform.system", lambda: platform_name)
    version_path = tmp_path / "versions" / "Test"
    version_path.mkdir(parents=True)
    processor_path = tmp_path / "libraries/test/processor/1/processor-1.jar"
    processor_path.parent.mkdir(parents=True)
    with zipfile.ZipFile(processor_path, "w") as archive:
        archive.writestr("META-INF/MANIFEST.MF", "Main-Class: Test\n")
    profile = {"data": {"BINPATCH": {"client": "/patch.bin"}, "PATCHED": {"client": "[test:patched:1]"}},
               "processors": [{"jar": "test:processor:1", "classpath": ["test:helper:1"], "args": []}]}
    installer = tmp_path / "installer.jar"
    with zipfile.ZipFile(installer, "w") as archive:
        archive.writestr("install_profile.json", json.dumps(profile))
        archive.writestr("version.json", json.dumps({"inheritsFrom": "1.20.1"}))
        archive.writestr("patch.bin", b"patch")
    output, events, commands = [], [], []

    def execute(**kwargs):
        commands.append(kwargs["args"])
        kwargs["log_callback"]("processor output", "private-instance-id")
        return "private-instance-id", SimpleNamespace(returncode=exit_code)

    checker = SimpleNamespace(config=None, check_libraries=Mock(return_value=[]))
    loader = LoaderInstaller(checker, SimpleNamespace(create_instance=execute), tmp_path, log_callback=output.append)
    if exit_code:
        with pytest.raises(RuntimeError, match="第 1/1 个处理器退出码 1"):
            loader.install_neoforged(installer, "java", "Test", observer=InstallObserver(events.append))
        assert not any(event.stage == "install_loader" and event.status == "completed" for event in events)
        assert not (version_path / "Test.json").exists()
    else:
        loader.install_neoforged(installer, "java", "Test", observer=InstallObserver(events.append))
        assert any(event.unit == "items" and event.completed == 1 and event.total == 1 for event in events)
        assert (version_path / "Test.json").is_file()
    assert output == ["processor output"]
    separator = ";" if platform_name == "Windows" else ":"
    helper_path = tmp_path / "libraries/test/helper/1/helper-1.jar"
    classpath = commands[0][commands[0].index("-cp") + 1]
    assert classpath == f"{helper_path}{separator}{processor_path}"


def test_processor_resolves_installer_data_placeholders(tmp_path, monkeypatch):
    """处理器参数里的 data 变量应解析成 libraries 路径，字面量保持原样。"""
    monkeypatch.setattr("ECL.game.Core.LoaderInstaller.platform.system", lambda: "Windows")
    version_path = tmp_path / "versions" / "Test"
    version_path.mkdir(parents=True)
    processor_path = tmp_path / "libraries/test/processor/1/processor-1.jar"
    processor_path.parent.mkdir(parents=True)
    with zipfile.ZipFile(processor_path, "w") as archive:
        archive.writestr("META-INF/MANIFEST.MF", "Main-Class: Test\n")
    profile = {
        "data": {
            "BINPATCH": {"client": "/patch.bin"},
            "PATCHED": {"client": "[test:patched:1]"},
            "MAPPINGS": {"client": "[test:neoform:1:mappings@txt]"},
            "LABEL": {"client": "'literal value'"},
        },
        "processors": [
            {
                "jar": "test:processor:1",
                "classpath": ["test:processor:1"],
                "args": ["--input", "{MAPPINGS}", "--output", "{PATCHED}", "--label", "{LABEL}", "{SIDE}"],
            }
        ],
    }
    installer = tmp_path / "installer.jar"
    with zipfile.ZipFile(installer, "w") as archive:
        archive.writestr("install_profile.json", json.dumps(profile))
        archive.writestr("version.json", json.dumps({"inheritsFrom": "1.20.1"}))
        archive.writestr("patch.bin", b"patch")
    commands = []

    def execute(**kwargs):
        commands.append(kwargs["args"])
        return "private-instance-id", SimpleNamespace(returncode=0)

    checker = SimpleNamespace(config=None, check_libraries=Mock(return_value=[]))
    loader = LoaderInstaller(checker, SimpleNamespace(create_instance=execute), tmp_path, log_callback=lambda _line: None)
    loader.install_neoforged(installer, "java", "Test", observer=InstallObserver(lambda _event: None))

    assert commands[0][-7:] == [
        "--input",
        str(tmp_path / "libraries/test/neoform/1/neoform-1-mappings.txt"),
        "--output",
        str(tmp_path / "libraries/test/patched/1/patched-1.jar"),
        "--label",
        "literal value",
        "client",
    ]


def test_processor_rejects_unknown_placeholder(tmp_path, monkeypatch):
    """参数里出现未声明的占位符时应直接失败，不把占位符交给 Java。"""
    monkeypatch.setattr("ECL.game.Core.LoaderInstaller.platform.system", lambda: "Windows")
    version_path = tmp_path / "versions" / "Test"
    version_path.mkdir(parents=True)
    processor_path = tmp_path / "libraries/test/processor/1/processor-1.jar"
    processor_path.parent.mkdir(parents=True)
    with zipfile.ZipFile(processor_path, "w") as archive:
        archive.writestr("META-INF/MANIFEST.MF", "Main-Class: Test\n")
    profile = {
        "data": {"BINPATCH": {"client": "/patch.bin"}, "PATCHED": {"client": "[test:patched:1]"}},
        "processors": [{"jar": "test:processor:1", "classpath": ["test:processor:1"], "args": ["{UNKNOWN_TOKEN}"]}],
    }
    installer = tmp_path / "installer.jar"
    with zipfile.ZipFile(installer, "w") as archive:
        archive.writestr("install_profile.json", json.dumps(profile))
        archive.writestr("version.json", json.dumps({"inheritsFrom": "1.20.1"}))
        archive.writestr("patch.bin", b"patch")

    def execute(**kwargs):
        raise AssertionError("参数解析失败时不应启动处理器")

    checker = SimpleNamespace(config=None, check_libraries=Mock(return_value=[]))
    loader = LoaderInstaller(checker, SimpleNamespace(create_instance=execute), tmp_path, log_callback=lambda _line: None)
    with pytest.raises(RuntimeError, match="未知占位符"):
        loader.install_neoforged(installer, "java", "Test", observer=InstallObserver(lambda _event: None))


@pytest.mark.parametrize("merge_fails", [False, True])
async def test_chunks_retry_only_failed_part_and_clean_up_after_merge(monkeypatch, tmp_path, merge_fails):
    original_client = httpx.AsyncClient
    payload = b"x" * (2 * 1024 * 1024)
    requests = []

    def handler(request):
        if request.method == "HEAD":
            return httpx.Response(200, headers={"content-length": str(len(payload)), "accept-ranges": "bytes"})
        part = request.headers["Range"]
        requests.append(part)
        if part.startswith("bytes=0-") and requests.count(part) == 1:
            raise httpx.ReadError("interrupted part")
        start, end = map(int, part.removeprefix("bytes=").split("-"))
        return httpx.Response(206, content=payload[start:end + 1])

    monkeypatch.delenv("ECL_DOWNLOAD_PROXY", raising=False)
    monkeypatch.setattr("ECL.game.Core.Downloader.httpx.AsyncClient",
                        lambda **kw: original_client(transport=httpx.MockTransport(handler), **kw))
    target = tmp_path / "file"
    target.write_bytes(b"existing")
    original_replace = Path.replace

    def replace(source, destination):
        if merge_fails and destination == target:
            raise OSError("cannot replace target")
        return original_replace(source, destination)

    monkeypatch.setattr(Path, "replace", replace)
    downloader = Downloader([("http://example.test/file", target)], skip_preflight=True,
                            chunk_size_mb=1, min_chunk_mb=1, chunk_threshold_mb=0.1, max_rounds=1)
    await downloader.run()
    assert requests.count("bytes=0-1048575") == 2
    assert requests.count("bytes=1048576-2097151") == 1
    assert target.read_bytes() == (b"existing" if merge_fails else payload)
    assert bool(downloader.failed_entries) is merge_fails
    assert not list(tmp_path.glob("*.tmp"))
