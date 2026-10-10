from __future__ import annotations

import json
from pathlib import Path

import pytest
from ECL.game import InstanceMetadata


def test_metadata_retains_parent_java_and_all_components_without_jar(tmp_path: Path) -> None:
    parent = tmp_path / "versions" / "26.4-snapshot-3"
    child = tmp_path / "versions" / "fabric"
    parent.mkdir(parents=True)
    child.mkdir()
    (parent / "26.4-snapshot-3.json").write_text(
        json.dumps({"id": "26.4-snapshot-3", "javaVersion": {"majorVersion": 25}}), encoding="utf-8"
    )
    (child / "fabric.json").write_text(
        json.dumps(
            {
                "id": "fabric",
                "inheritsFrom": "26.4-snapshot-3",
                "libraries": [{"name": "net.fabricmc:fabric-loader:0.17.0"}],
            }
        ),
        encoding="utf-8",
    )
    metadata = InstanceMetadata.read(tmp_path, "fabric")
    assert [document["id"] for document in metadata.documents] == ["fabric", "26.4-snapshot-3"]
    assert metadata.documents[1]["javaVersion"] == {"majorVersion": 25}
    assert metadata.components == (("Fabric", "0.17.0"),)
    assert metadata.mod_environment() == {"minecraft": "26.4-snapshot-3", "java": None, "fabricloader": "0.17.0"}


@pytest.mark.parametrize("content", ["broken", "[]", '{"id": 1}'])
def test_unreadable_metadata_returns_empty_result_without_writing(tmp_path: Path, content: str) -> None:
    version = tmp_path / "versions" / "test"
    version.mkdir(parents=True)
    version_file = version / "test.json"
    version_file.write_text(content, encoding="utf-8")
    assert InstanceMetadata.read(tmp_path, "test") == InstanceMetadata((), ())
    assert version_file.read_text(encoding="utf-8") == content
