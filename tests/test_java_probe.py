from pathlib import Path
from types import SimpleNamespace

import pytest

from ECL.game import JavaScanner


@pytest.mark.parametrize("is_jdk", [False, True])
def test_modular_jre_is_not_a_jdk_and_probe_has_no_cache_side_effects(tmp_path, monkeypatch, is_jdk):
    from ECL.game.Utils import JavaScanner as scanner_module

    java_home = tmp_path / "runtime"
    (java_home / "bin").mkdir(parents=True)
    (java_home / "lib").mkdir()
    (java_home / "lib" / "modules").write_bytes(b"modular runtime")
    executable = java_home / "bin" / ("java.exe" if scanner_module.sys.platform == "win32" else "java")
    executable.write_bytes(b"java")
    if is_jdk:
        executable.with_name("javac.exe" if scanner_module.sys.platform == "win32" else "javac").write_bytes(b"compiler")
    output = f"java.version = 21.0.2\njava.vendor = Eclipse Adoptium\nos.arch = amd64\njava.home = {java_home}\n"
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stderr=output, stdout="")

    monkeypatch.setattr(scanner_module.subprocess, "run", run)
    result = JavaScanner.probe(executable)
    assert result.version == "21.0.2" and result.is_jdk is is_jdk
    assert calls[0][1]["timeout"] == 10
    assert not (java_home / "java_cache.json").exists()


@pytest.mark.parametrize(("exit_code", "output"), [(1, "java.version = 21\nos.arch = amd64"), (0, "java.version = unknown\nos.arch = amd64"), (0, "java.version = 21")])
def test_probe_rejects_failed_or_incomplete_output(tmp_path, monkeypatch, exit_code, output):
    from ECL.game.Utils import JavaScanner as scanner_module

    executable = tmp_path / ("java.exe" if scanner_module.sys.platform == "win32" else "java")
    executable.write_bytes(b"java")
    monkeypatch.setattr(scanner_module.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=exit_code, stderr=output, stdout=""))
    assert JavaScanner.probe(executable) is None


def test_java8_outer_jdk_uses_sibling_compiler_when_java_home_points_to_jre(tmp_path, monkeypatch):
    from ECL.game.Utils import JavaScanner as scanner_module

    java_home = tmp_path / "jdk"
    (java_home / "bin").mkdir(parents=True)
    suffix = ".exe" if scanner_module.sys.platform == "win32" else ""
    executable = java_home / "bin" / f"java{suffix}"
    executable.write_bytes(b"java")
    executable.with_name(f"javac{suffix}").write_bytes(b"compiler")
    monkeypatch.setattr(scanner_module.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0, stderr=f"java.version = 1.8.0_451\nos.arch = amd64\njava.home = {java_home / 'jre'}\n", stdout=""))
    assert JavaScanner.probe(executable).is_jdk
