import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from ECL.game.Core.Downloader import Downloader
from ECL.game.Core.FilesChecker import FilesChecker
from ECL.game.Core.NetLibs import ApiUrlConfig


def test_asset_index_request_failure_is_not_silently_ignored(tmp_path):
    failure = httpx.ConnectError("asset index unavailable")
    client = SimpleNamespace(config=ApiUrlConfig(), get_asset_index=Mock(side_effect=failure))
    checker = FilesChecker(client)

    with pytest.raises(httpx.ConnectError, match="asset index unavailable"):
        checker.check_assets(tmp_path, {"assetIndex": {"id": "1.21", "sha1": "0" * 40}})


def test_downloader_records_local_write_failure(tmp_path, monkeypatch):
    target = tmp_path / "unwritable" / "client.jar"
    original_mkdir = type(target.parent).mkdir

    def fail_target_directory(path, *args, **kwargs):
        if path == target.parent:
            raise OSError("disk unavailable")
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(type(target.parent), "mkdir", fail_target_directory)

    async def run():
        transport = httpx.MockTransport(lambda _request: httpx.Response(200, content=b"jar"))
        async with httpx.AsyncClient(transport=transport) as client:
            downloader = Downloader([])
            downloader.client = client
            assert await downloader._download_stream("https://example.test/client.jar", target) is False
            assert str(target) in downloader.local_failed_paths

    asyncio.run(run())
