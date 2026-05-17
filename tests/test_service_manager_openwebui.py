"""Open WebUI 起動状態の回帰テスト。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from service_manager import ServiceInfo, ServiceManager, ServiceStatus


class DummyProcess:
    """subprocess.Popen の最小代替オブジェクト。"""

    def __init__(self, pid: int = 12345, exit_code: int | None = None) -> None:
        self.pid = pid
        self.returncode = exit_code

    def poll(self) -> int | None:
        """終了していなければ None、終了済みなら終了コードを返す。"""
        return self.returncode


def make_manager(tmp_path: Path, port: int = 3000) -> ServiceManager:
    """テスト用 ServiceManager を作成する。"""
    settings = {
        "pipeline": {
            "state_file": str(tmp_path / "pipeline_state.json"),
            "lock_file": str(tmp_path / "pipeline.lock"),
        },
        "llm": {"host": "http://localhost:11434"},
        "openwebui": {"base_url": f"http://localhost:{port}"},
    }
    return ServiceManager(tmp_path, settings)


def make_sync_enabled_manager(tmp_path: Path, port: int = 3000) -> ServiceManager:
    """Open WebUI 同期が有効なテスト用 ServiceManager を作成する。"""
    settings = {
        "pipeline": {
            "state_file": str(tmp_path / "pipeline_state.json"),
            "lock_file": str(tmp_path / "pipeline.lock"),
        },
        "llm": {"host": "http://localhost:11434"},
        "openwebui": {
            "enabled": True,
            "base_url": f"http://localhost:{port}",
            "knowledge_id": "knowledge-test-id",
        },
    }
    return ServiceManager(tmp_path, settings)


def create_webui_exe(tmp_path: Path) -> None:
    """start_open_webui の存在チェックを通すためのダミー exe を作る。"""
    scripts_dir = tmp_path / ".venv-webui" / "Scripts"
    scripts_dir.mkdir(parents=True)
    (scripts_dir / "open-webui.exe").write_text("", encoding="utf-8")


def test_check_open_webui_reports_starting_for_managed_process(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """HTTP 応答前でも管理中プロセスが生きていれば起動中扱いにする。"""
    import requests

    mgr = make_manager(tmp_path)
    mgr._processes["Open WebUI"] = DummyProcess(pid=24680)  # noqa: SLF001

    def fake_get(*_args: Any, **_kwargs: Any) -> Any:
        raise requests.exceptions.ConnectionError()

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(ServiceManager, "_find_listening_pids", staticmethod(lambda _port: []))

    info = mgr.check_open_webui()

    assert info.status == ServiceStatus.UNKNOWN
    assert info.pid == 24680
    assert "起動中" in info.detail


def test_start_open_webui_does_not_spawn_duplicate_while_starting(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """起動中判定の間は重複して open-webui.exe を起動しない。"""
    create_webui_exe(tmp_path)
    mgr = make_manager(tmp_path)

    monkeypatch.setattr(
        mgr,
        "check_open_webui",
        lambda: ServiceInfo(
            name="Open WebUI",
            status=ServiceStatus.UNKNOWN,
            detail="起動中",
            pid=24680,
        ),
    )

    def fail_popen(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("Popen should not be called while Open WebUI is starting")

    monkeypatch.setattr(subprocess, "Popen", fail_popen)

    ok, msg = mgr.start_open_webui()

    assert ok is True
    assert "起動中" in msg
    assert "24680" in msg


def test_check_open_webui_reports_stopped_for_nonzero_launcher_exit(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """管理中プロセスが異常終了した場合は停止扱いにする。"""
    import requests

    mgr = make_manager(tmp_path)
    mgr._processes["Open WebUI"] = DummyProcess(pid=24680, exit_code=1)  # noqa: SLF001

    def fake_get(*_args: Any, **_kwargs: Any) -> Any:
        raise requests.exceptions.ConnectionError()

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(ServiceManager, "_find_listening_pids", staticmethod(lambda _port: []))

    info = mgr.check_open_webui()

    assert info.status == ServiceStatus.STOPPED
    assert "終了コード=1" in info.detail


def test_start_open_webui_reports_immediate_exit_as_failure(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """起動直後に open-webui.exe が終了した場合は成功扱いにしない。"""
    create_webui_exe(tmp_path)
    mgr = make_manager(tmp_path)

    monkeypatch.setattr(
        mgr,
        "check_open_webui",
        lambda: ServiceInfo(
            name="Open WebUI",
            status=ServiceStatus.STOPPED,
            detail="停止中",
        ),
    )

    def fake_popen(_cmd: list[str], **_kwargs: Any) -> DummyProcess:
        return DummyProcess(pid=86420, exit_code=1)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    ok, msg = mgr.start_open_webui()

    assert ok is False
    assert "終了コード=1" in msg


def test_start_open_webui_uses_configured_port_and_project_root(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """Open WebUI 起動時は設定 port と project_root を Popen に渡す。"""
    create_webui_exe(tmp_path)
    mgr = make_manager(tmp_path, port=3456)
    captured: dict[str, Any] = {}

    monkeypatch.setattr(
        mgr,
        "check_open_webui",
        lambda: ServiceInfo(
            name="Open WebUI",
            status=ServiceStatus.STOPPED,
            detail="停止中",
        ),
    )

    def fake_popen(cmd: list[str], **kwargs: Any) -> DummyProcess:
        captured["cmd"] = cmd
        captured["cwd"] = kwargs.get("cwd")
        return DummyProcess(pid=13579)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    ok, msg = mgr.start_open_webui()

    assert ok is True
    assert captured["cmd"][-2:] == ["--port", "3456"]
    assert captured["cwd"] == str(tmp_path)
    assert "13579" in msg


def test_start_open_webui_forces_utf8_stdout_environment(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """Windows cp932 で Open WebUI の Unicode バナーが落ちないよう UTF-8 を渡す。"""
    create_webui_exe(tmp_path)
    mgr = make_manager(tmp_path)
    captured: dict[str, Any] = {}

    monkeypatch.setattr(
        mgr,
        "check_open_webui",
        lambda: ServiceInfo(
            name="Open WebUI",
            status=ServiceStatus.STOPPED,
            detail="停止中",
        ),
    )

    def fake_popen(_cmd: list[str], **kwargs: Any) -> DummyProcess:
        captured["env"] = kwargs.get("env")
        return DummyProcess(pid=97531)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    ok, _msg = mgr.start_open_webui()

    assert ok is True
    assert captured["env"]["PYTHONIOENCODING"] == "utf-8"
    assert captured["env"]["PYTHONUTF8"] == "1"


def test_start_open_webui_queues_pending_sync_when_enabled(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """Open WebUI 起動後は未同期 notes の同期をバックグラウンド予約する。"""
    create_webui_exe(tmp_path)
    mgr = make_sync_enabled_manager(tmp_path)
    queued: list[bool] = []

    monkeypatch.setattr(
        mgr,
        "check_open_webui",
        lambda: ServiceInfo(
            name="Open WebUI",
            status=ServiceStatus.STOPPED,
            detail="停止中",
        ),
    )

    def fake_popen(_cmd: list[str], **_kwargs: Any) -> DummyProcess:
        return DummyProcess(pid=11223)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(mgr, "_queue_open_webui_pending_sync", lambda: queued.append(True) or True)

    ok, _msg = mgr.start_open_webui()

    assert ok is True
    assert queued == [True]


def test_notes_auto_sync_waits_until_markdown_file_is_stable(tmp_path: Path) -> None:
    """notes 監視は .md のサイズ/mtime が一定時間安定してから同期対象にする。"""
    mgr = make_sync_enabled_manager(tmp_path)
    notes_dir = tmp_path / "data" / "notes"
    notes_dir.mkdir(parents=True)
    mgr._notes_dir = notes_dir  # noqa: SLF001
    mgr._notes_stable_seconds = 5.0  # noqa: SLF001
    mgr._notes_file_states = mgr._collect_notes_file_states(now=0.0)  # noqa: SLF001

    note_path = notes_dir / "new_note.md"
    note_path.write_text("作成中", encoding="utf-8")

    assert mgr._scan_notes_for_stable_changes(now=1.0) is False  # noqa: SLF001
    assert mgr._scan_notes_for_stable_changes(now=7.0) is True  # noqa: SLF001
    assert str(note_path.resolve()) in mgr._notes_pending_paths  # noqa: SLF001

    mgr._clear_stable_notes_pending(now=7.0)  # noqa: SLF001

    assert str(note_path.resolve()) not in mgr._notes_pending_paths  # noqa: SLF001
