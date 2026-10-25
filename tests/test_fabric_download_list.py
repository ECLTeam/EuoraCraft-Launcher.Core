from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, Mock

from ECL.game import GetGames


def test_fabric_list_contains_loader_dependencies_and_writes_metadata(tmp_path: Path) -> None:
    client = SimpleNamespace(
        get_minecraft_manifest=Mock(
            return_value={"latest": {}, "versions": [{"id": "26.3", "type": "release", "sha1": "hash"}]}
        ),
        get_minecraft_json=Mock(return_value={"id": "26.3"}),
        get_fabric_profile=Mock(return_value={"id": "fabric", "libraries": []}),
    )
    dependency = ("https://example.test/loader.jar", str(tmp_path / "libraries/loader.jar"))
    checker = SimpleNamespace(config=None, api_client=client, check_files=Mock(return_value=[dependency]))
    games = GetGames(checker, None, tmp_path)
    assert games.build_fabric_download_list("26.3", "0.16", "Fabric") == [dependency]
    metadata = json.loads((tmp_path / "versions/Fabric/VersionInfo.json").read_text(encoding="utf-8"))
    assert metadata["LoaderType"] == "Fabric"
    assert metadata["VanillaVersion"] == "26.3"
    checker.check_files.assert_called_once_with(tmp_path, "Fabric", observer=ANY)
    assert not (tmp_path / "mods").exists()
