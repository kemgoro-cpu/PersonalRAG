"""ServiceManager の Pipeline 死活判定テスト。"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from service_manager import ServiceManager, ServiceStatus


def make_manager(tmp_path: Path, state_file: Path, lock_file: Path) -> ServiceManager:
    """テスト用 ServiceManager を作成する。"""
    settings = {
        "pipeline": {
            "state_file": str(state_file),
            "lock_file": str(lock_file),
        },
        "llm": {"host": "http://localhost:11434"},
        "openwebui": {"base_url": "http://localhost:3000"},
    }
    return ServiceManager(tmp_path, settings)


def write_state(path: Path, seconds_ago: int) -> None:
    """updated_at を指定秒数前にした状態ファイルを書く。"""
    updated_at = (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()
    path.write_text(
        json.dumps({"updated_at": updated_at, "current": None, "queue": [], "recent": []}),
        encoding="utf-8",
    )


def test_fresh_state_without_lock_is_stopped(tmp_path: Path) -> None:
    """状態ファイルが新しくても lock PID がなければ停止扱いにする。"""
    state_file = tmp_path / "pipeline_state.json"
    lock_file = tmp_path / "pipeline.lock"
    write_state(state_file, seconds_ago=5)

    info = make_manager(tmp_path, state_file, lock_file).check_pipeline()

    assert info.status == ServiceStatus.STOPPED
    assert "lock PID" in info.detail


def test_fresh_state_with_running_lock_pid_is_running(tmp_path: Path) -> None:
    """状態ファイルが新しく lock PID が生存中なら稼働中にする。"""
    state_file = tmp_path / "pipeline_state.json"
    lock_file = tmp_path / "pipeline.lock"
    write_state(state_file, seconds_ago=5)
    lock_file.write_text(str(os.getpid()), encoding="utf-8")

    info = make_manager(tmp_path, state_file, lock_file).check_pipeline()

    assert info.status == ServiceStatus.RUNNING
    assert info.pid == os.getpid()


def test_stale_state_with_running_lock_pid_is_stopped(tmp_path: Path) -> None:
    """lock PID が生存中でも状態ファイルが古ければ停止扱いにする。"""
    state_file = tmp_path / "pipeline_state.json"
    lock_file = tmp_path / "pipeline.lock"
    write_state(state_file, seconds_ago=120)
    lock_file.write_text(str(os.getpid()), encoding="utf-8")

    info = make_manager(tmp_path, state_file, lock_file).check_pipeline()

    assert info.status == ServiceStatus.STOPPED
    assert "最終更新" in info.detail
