import subprocess
import sys
from pathlib import Path
from time import monotonic

import pytest
from ECL.game import JavaProbeError
from ECL.game.Utils.java_probe import probe_output


def run_script(monkeypatch, script):
    from ECL.game.Utils import java_probe

    original = subprocess.Popen
    processes = []
    arguments = []

    def start(args, **kwargs):
        arguments.append((args, kwargs))
        process = original([sys.executable, "-c", script], **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(java_probe.subprocess, "Popen", start)
    return processes, arguments


def test_real_output_is_bounded_and_process_is_reaped(monkeypatch):
    processes, arguments = run_script(monkeypatch, "import sys; sys.stdout.write('x' * 200000); sys.stdout.flush()")
    with pytest.raises(JavaProbeError) as error:
        probe_output(Path("java.exe"), monotonic() + 5, 1024, {}, None)
    assert error.value.code == "output_limit"
    assert processes[0].poll() is not None
    assert processes[0].stdout.closed
    assert arguments[0][0][1:] == ["-XshowSettings:properties", "-version"]
    assert arguments[0][1]["stdin"] == subprocess.DEVNULL
    assert arguments[0][1]["env"] == {}


def test_real_timeout_kills_only_created_probe(monkeypatch):
    processes, _ = run_script(monkeypatch, "import time; time.sleep(60)")
    with pytest.raises(JavaProbeError) as error:
        probe_output(Path("java.exe"), monotonic() + 0.15, 65536, None, None)
    assert error.value.code == "deadline"
    assert processes[0].poll() is not None
    assert processes[0].stdout.closed


def test_revocation_after_start_reaps_actual_process(monkeypatch):
    processes, _ = run_script(monkeypatch, "import time; time.sleep(60)")

    def check():
        if processes:
            raise RuntimeError("revoked")

    with pytest.raises(RuntimeError, match="revoked"):
        probe_output(Path("java.exe"), monotonic() + 5, 65536, None, check)
    assert processes[0].poll() is not None


def test_already_expired_probe_never_starts(monkeypatch):
    processes, _ = run_script(monkeypatch, "raise AssertionError('must not run')")
    with pytest.raises(JavaProbeError):
        probe_output(Path("java.exe"), monotonic() - 1, 65536, None, None)
    assert not processes


def test_public_probe_preserves_revocation_from_check_and_reaps_process(tmp_path, monkeypatch):
    from ECL.game import JavaScanner

    executable = tmp_path / ("java.exe" if sys.platform == "win32" else "java")
    executable.write_bytes(b"fixture")
    processes, _ = run_script(monkeypatch, "import time; time.sleep(60)")

    def check():
        if processes:
            raise ValueError("revoked")

    with pytest.raises(ValueError, match="revoked"):
        JavaScanner.probe(executable, deadline=monotonic() + 5, check=check)
    assert processes[0].poll() is not None


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_probe_process_platform_flags_and_successful_cleanup(monkeypatch, platform):
    from types import SimpleNamespace

    from ECL.game.Utils import java_probe

    monkeypatch.setattr(java_probe, "sys", SimpleNamespace(platform=platform))
    if not hasattr(subprocess, "CREATE_NO_WINDOW"):
        monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0, raising=False)
    processes, arguments = run_script(monkeypatch, "print('java.version = 25')")
    assert probe_output(Path("java"), monotonic() + 5, 65536, None, None)
    assert ("creationflags" in arguments[0][1]) == (platform == "win32")
    assert processes[0].poll() == 0 and processes[0].stdout.closed
