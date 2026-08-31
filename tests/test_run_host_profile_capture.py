from __future__ import annotations

import inspect

from tools import run_host_profile


def test_host_profile_runner_uses_file_backed_capture() -> None:
    source = inspect.getsource(run_host_profile.main)
    assert "tempfile.TemporaryFile" in source
    assert "stdout=stdout_file" in source
    assert "stderr=stderr_file" in source
    assert "capture_output=True" not in source
