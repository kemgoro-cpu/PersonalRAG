"""外部起動サービス停止の回帰テスト。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from service_manager import ServiceManager, ServiceStatus


def make_manager(tmp_path: Path) -> ServiceManager:
    """テスト用 ServiceManager を作成する。"""
    settings = {
        "pipeline": {
            "state_file": str(tmp_path / "pipeline_state.json"),
            "lock_file": str(tmp_path / "pipeline.lock"),
        },
        "llm": {"host": "http://localhost:11434"},
        "openwebui": {"base_url": "http://localhost:3000"},
    }
    return ServiceManager(tmp_path, settings)


def test_stop_service_stops_detected_external_pid(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """管理中 Popen がなくても、検出できた外部 PID を taskkill する。"""
    mgr = make_manager(tmp_path)
    killed: list[list[str]] = []

    monkeypatch.setattr(mgr, "_detect_external_pids", lambda _name: [12345])
    monkeypatch.setattr(ServiceManager, "_is_pid_running", staticmethod(lambda _pid: True))

    def fake_run(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        killed.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    ok, msg = mgr.stop_service("Pipeline")

    assert ok is True
    assert "12345" in msg
    assert killed == [["taskkill", "/PID", "12345", "/T", "/F"]]


def test_stop_service_treats_stale_external_pid_as_already_stopped(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """検出済み PID が既に消えていた場合は停止済みとして成功扱いにする。"""
    mgr = make_manager(tmp_path)

    monkeypatch.setattr(mgr, "_detect_external_pids", lambda _name: [39384])
    monkeypatch.setattr(ServiceManager, "_is_pid_running", staticmethod(lambda _pid: False))

    ok, msg = mgr.stop_service("Pipeline")

    assert ok is True
    assert "既に停止" in msg
    assert "39384" in msg


def test_stop_service_treats_taskkill_not_found_as_stopped(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """taskkill 時点で PID が消えていた場合は停止済みとして成功扱いにする。"""
    mgr = make_manager(tmp_path)
    running_checks = iter([True, False])

    monkeypatch.setattr(mgr, "_detect_external_pids", lambda _name: [39384])
    monkeypatch.setattr(
        ServiceManager,
        "_is_pid_running",
        staticmethod(lambda _pid: next(running_checks)),
    )

    def fake_run(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            cmd,
            128,
            stdout='エラー: プロセス "39384" が見つかりませんでした。',
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    ok, msg = mgr.stop_service("Pipeline")

    assert ok is True
    assert "39384" in msg


def test_stop_all_targets_running_external_services(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """すべて停止は外部起動の RUNNING サービスも停止対象に含める。"""
    mgr = make_manager(tmp_path)
    stopped: list[str] = []

    monkeypatch.setattr(
        mgr,
        "check_all",
        lambda: [
            type("Info", (), {"name": "Ollama", "status": ServiceStatus.RUNNING})(),
            type("Info", (), {"name": "Pipeline", "status": ServiceStatus.STOPPED})(),
            type("Info", (), {"name": "Open WebUI", "status": ServiceStatus.RUNNING})(),
        ],
    )

    def fake_stop(name: str) -> tuple[bool, str]:
        stopped.append(name)
        return True, f"{name} stopped"

    monkeypatch.setattr(mgr, "stop_service", fake_stop)

    results = mgr.stop_all()

    assert stopped == ["Ollama", "Open WebUI"]
    assert set(results) == {"Ollama", "Open WebUI"}
