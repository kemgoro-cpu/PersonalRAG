"""test_service_manager_standalone.py
ServiceManager の単体テスト（Ollama / Open WebUI を起動していない状態で実施）。

実行方法:
    python scripts/test_service_manager_standalone.py

このテストは:
- Ollama が起動していない環境で check_ollama() が STOPPED を返すことを確認
- Open WebUI が起動していない環境で check_open_webui() が STOPPED を返すことを確認
- pipeline_state.json が存在しない場合に check_pipeline() が STOPPED を返すことを確認
- pipeline_state.json の updated_at が 60 秒以上前の場合に STOPPED を返すことを確認
- pipeline_state.json の updated_at が 5 秒前かつ lock PID 生存中の場合に RUNNING を返すことを確認
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

# scripts/ を import パスに追加（スクリプト直接実行向け）
scripts_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(scripts_dir))

from service_manager import ServiceManager, ServiceStatus


def make_manager(
    state_file: Path | None = None,
    lock_file: Path | None = None,
) -> ServiceManager:
    """テスト用の ServiceManager を作成するヘルパー。"""
    project_root = Path(__file__).resolve().parent.parent
    if lock_file is None and state_file is not None:
        lock_file = state_file.with_suffix(".lock")
    settings: dict = {
        "pipeline": {
            "state_file": str(state_file) if state_file else "data/logs/pipeline_state.json",
            "lock_file": str(lock_file) if lock_file else "data/logs/pipeline.lock",
        },
        "llm": {"host": "http://localhost:11434"},
        "openwebui": {"base_url": "http://localhost:3000"},
    }
    mgr = ServiceManager(project_root, settings)
    # 状態ファイルのパスを直接上書き（設定ファイルを経由せずにテスト）
    if state_file is not None:
        mgr._pipeline_state_file = state_file
    if lock_file is not None:
        mgr._pipeline_lock_file = lock_file
    return mgr


def test_ollama_stopped() -> None:
    """Ollama が起動していない場合 STOPPED が返ることを確認する。"""
    mgr = make_manager()
    info = mgr.check_ollama()
    # 起動していない環境では STOPPED になるはず
    # （起動している場合はテストをスキップ）
    if info.status == ServiceStatus.RUNNING:
        print("  SKIP: Ollama は既に起動中のためスキップ")
        return
    assert info.status == ServiceStatus.STOPPED, f"Expected STOPPED, got {info.status}"
    assert info.name == "Ollama"
    print("  PASS: Ollama STOPPED")


def test_webui_stopped() -> None:
    """Open WebUI が起動していない場合 STOPPED が返ることを確認する。"""
    mgr = make_manager()
    info = mgr.check_open_webui()
    if info.status == ServiceStatus.RUNNING:
        print("  SKIP: Open WebUI は既に起動中のためスキップ")
        return
    assert info.status == ServiceStatus.STOPPED, f"Expected STOPPED, got {info.status}"
    assert info.name == "Open WebUI"
    print("  PASS: Open WebUI STOPPED")


def test_pipeline_no_state_file() -> None:
    """状態ファイルが存在しない場合 STOPPED が返ることを確認する。"""
    # 存在しないパスを指定
    nonexistent = Path(tempfile.gettempdir()) / "nonexistent_state_xyzabc.json"
    mgr = make_manager(state_file=nonexistent)
    info = mgr.check_pipeline()
    assert info.status == ServiceStatus.STOPPED, f"Expected STOPPED, got {info.status}"
    assert info.name == "Pipeline"
    print("  PASS: Pipeline STOPPED (no state file)")


def test_pipeline_stale_state_file() -> None:
    """updated_at が 60 秒以上前の場合 STOPPED が返ることを確認する。"""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as f:
        old_time = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
        json.dump({"updated_at": old_time, "current": None, "queue": [], "recent": []}, f)
        tmp_path = Path(f.name)

    try:
        mgr = make_manager(state_file=tmp_path)
        info = mgr.check_pipeline()
        assert info.status == ServiceStatus.STOPPED, f"Expected STOPPED, got {info.status}"
        print("  PASS: Pipeline STOPPED (stale state file)")
    finally:
        tmp_path.unlink(missing_ok=True)


def test_pipeline_fresh_state_file_without_lock() -> None:
    """updated_at が 5 秒前でも lock がなければ STOPPED が返ることを確認する。"""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as f:
        fresh_time = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
        json.dump({"updated_at": fresh_time, "current": None, "queue": [], "recent": []}, f)
        tmp_path = Path(f.name)

    try:
        mgr = make_manager(state_file=tmp_path)
        info = mgr.check_pipeline()
        assert info.status == ServiceStatus.STOPPED, f"Expected STOPPED, got {info.status}"
        print("  PASS: Pipeline STOPPED (fresh state file without lock)")
    finally:
        tmp_path.unlink(missing_ok=True)


def test_pipeline_fresh_state_file_with_lock() -> None:
    """updated_at が 5 秒前かつ lock PID が生存中なら RUNNING が返ることを確認する。"""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as f:
        fresh_time = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
        json.dump({"updated_at": fresh_time, "current": None, "queue": [], "recent": []}, f)
        tmp_path = Path(f.name)

    lock_path = Path(tempfile.gettempdir()) / "pipeline_lock_live_pid_test.lock"
    lock_path.write_text(str(os.getpid()), encoding="utf-8")

    try:
        mgr = make_manager(state_file=tmp_path, lock_file=lock_path)
        info = mgr.check_pipeline()
        assert info.status == ServiceStatus.RUNNING, f"Expected RUNNING, got {info.status}"
        print("  PASS: Pipeline RUNNING (fresh state file with live lock)")
    finally:
        tmp_path.unlink(missing_ok=True)
        lock_path.unlink(missing_ok=True)


def test_check_all_returns_three_items() -> None:
    """check_all() が 3 つの ServiceInfo を返すことを確認する。"""
    mgr = make_manager()
    infos = mgr.check_all()
    assert len(infos) == 3, f"Expected 3 items, got {len(infos)}"
    names = [info.name for info in infos]
    assert "Ollama" in names
    assert "Pipeline" in names
    assert "Open WebUI" in names
    print("  PASS: check_all() returns 3 items")


def test_processes_dict_replaces_pids() -> None:
    """_processes 辞書が存在し、_pids が削除されていることを確認する（Medium 修正）。

    PID 再利用バグの修正として _pids を _processes（Popen 格納）に置き換えた。
    ServiceManager が正しく _processes 属性を持ち _pids を持たないことを検証する。
    """
    mgr = make_manager()
    assert hasattr(mgr, "_processes"), "_processes 属性が存在しない"
    assert not hasattr(mgr, "_pids"), "_pids 属性がまだ存在している（置き換えが不完全）"
    assert isinstance(mgr._processes, dict), "_processes が dict でない"
    print("  PASS: _processes dict exists, _pids removed")


def test_stop_service_when_not_started() -> None:
    """起動していないサービスを stop_service() しても安全に失敗することを確認する。

    _processes にエントリがない場合、taskkill は呼ばれず False が返る。
    """
    mgr = make_manager()
    mgr._detect_external_pids = lambda _name: []  # 実環境の外部サービスを止めない
    ok, msg = mgr.stop_service("Ollama")
    assert ok is False, f"起動していないのに ok=True が返った: {msg}"
    assert "停止対象 PID" in msg or "起動していません" in msg, f"メッセージが想定外: {msg}"
    print("  PASS: stop_service() on unstarted service returns False safely")


def test_stop_service_already_exited_popen() -> None:
    """Popen が即死後も stop_service() が安全に停止済みとして処理することを確認する（Medium 修正）。

    _processes に poll() != None の Popen を手動でセットし、
    stop_service() が taskkill を呼ばず True を返すことを確認する。
    これは「Popen 直後に即死 → PID 再利用 → 誤 kill」を防ぐ動作の検証。
    """
    import subprocess
    import threading as _threading

    mgr = make_manager()
    mgr._detect_external_pids = lambda _name: []  # 実環境の外部サービスを止めない

    # 即座に終了するプロセスを起動して Popen を取得する
    proc = subprocess.Popen(
        ["python", "-c", "pass"],  # 即終了する Python スクリプト
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # プロセスが終了するまで待機
    proc.wait(timeout=5)
    assert proc.poll() is not None, "テスト用プロセスが終了していない"

    # 終了済み Popen を手動で _processes に登録
    with mgr._lock:
        mgr._processes["Ollama"] = proc

    # stop_service() が taskkill を呼ばず、True（停止済み）を返すことを確認
    ok, msg = mgr.stop_service("Ollama")
    assert ok is True, f"既に終了した Popen に対して ok=False が返った: {msg}"
    assert "既に停止" in msg or "既に終了" in msg, f"メッセージが想定外: {msg}"

    # _processes からも除去されていることを確認
    with mgr._lock:
        assert "Ollama" not in mgr._processes, "_processes からエントリが削除されていない"

    print("  PASS: stop_service() on already-exited Popen returns True without taskkill")


def main() -> None:
    print("=== ServiceManager 単体テスト ===\n")
    tests = [
        test_ollama_stopped,
        test_webui_stopped,
        test_pipeline_no_state_file,
        test_pipeline_stale_state_file,
        test_pipeline_fresh_state_file_without_lock,
        test_pipeline_fresh_state_file_with_lock,
        test_check_all_returns_three_items,
        test_processes_dict_replaces_pids,
        test_stop_service_when_not_started,
        test_stop_service_already_exited_popen,
    ]

    passed = 0
    failed = 0
    for test in tests:
        print(f"[{test.__name__}]")
        try:
            test()
            passed += 1
        except AssertionError as e:
            print(f"  FAIL: {e}")
            failed += 1
        except Exception as e:
            print(f"  ERROR: {type(e).__name__}: {e}")
            failed += 1

    print(f"\n結果: {passed} 件成功 / {failed} 件失敗")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
