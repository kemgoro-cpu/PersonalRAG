"""test_phase_d_security.py
フェーズ D の Codex レビュー指摘 2 件に対する単体テスト。

  P1: search_lib.py のパストラバーサル修正を検証する
  P2: note_viewer.py のセマンティック検索 race condition 修正を検証する

実行:
    python scripts/test_phase_d_security.py
"""
from __future__ import annotations

import sys
import tempfile
import threading
import time
import types
import unittest.mock as mock
from pathlib import Path

# scripts/ ディレクトリを sys.path に追加して本番モジュールを import できるようにする
sys.path.insert(0, str(Path(__file__).parent))


# ===========================================================================
# P1: パストラバーサル修正の単体テスト
#
# search_lib.search_semantic() は chromadb / ollama / config_loader などに依存するため
# 直接呼ぶのは難しい。そこで「パストラバーサル対策ロジック自体」を関数として抽出して
# テストする。本番の search_lib.py の修正が正しく機能するかを検証する。
# ===========================================================================

def _resolve_note_path(notes_dir: Path, source_file: str) -> Path | None:
    """search_lib.search_semantic() 内のパス解決ロジックを再現した純粋関数。

    本番コードと同じロジックをここに写して、引数に対して期待通りに動作するかを
    テストする。本番コードを変更したら、このロジックも合わせて更新すること。

    Returns:
        解決できた Path（notes_dir 配下かつ実在する）または None。
    """
    if not source_file:
        return None

    # 二重防御 (1): basename だけを使う（ディレクトリ部分を捨てる）
    safe_name = Path(source_file).name
    candidate = notes_dir / safe_name

    # 二重防御 (2): resolve() 後に notes_dir 配下かチェック
    try:
        resolved = candidate.resolve(strict=True)  # 存在しなければ FileNotFoundError
        notes_root = notes_dir.resolve(strict=True)
        resolved.relative_to(notes_root)            # 配下でなければ ValueError
        return resolved
    except (FileNotFoundError, ValueError):
        note_path = None

    # フォールバック: .txt 拡張子 → .md を試す
    stem = Path(safe_name).stem
    candidate_md = notes_dir / f"{stem}.md"
    try:
        resolved_md = candidate_md.resolve(strict=True)
        notes_root_md = notes_dir.resolve(strict=True)
        resolved_md.relative_to(notes_root_md)
        return resolved_md
    except (FileNotFoundError, ValueError):
        return None


def test_path_traversal() -> bool:
    """パストラバーサル対策ロジックの単体テスト。"""
    print("=== P1: パストラバーサル対策テスト ===")
    all_ok = True

    with tempfile.TemporaryDirectory() as tmp:
        notes_dir = Path(tmp) / "notes"
        notes_dir.mkdir()

        # 正常なノートファイルを用意する
        (notes_dir / "rec_2026-05-15.md").write_text("テストノート", encoding="utf-8")

        # --- 正常系: 通常のファイル名 ---
        result = _resolve_note_path(notes_dir, "rec_2026-05-15.md")
        ok1 = result is not None and result.name == "rec_2026-05-15.md"
        status = "OK" if ok1 else f"FAIL (got={result})"
        print(f"  [{status}] 正常系: source_file='rec_2026-05-15.md' → notes_dir 配下が返る")
        all_ok = ok1 and all_ok

        # --- 攻撃系 1: Windows スタイルのパストラバーサル ---
        # "..\..\..\Windows\System32\drivers\etc\hosts" のようなパスが渡された場合
        # basename 化により "hosts" として扱われ、notes_dir/hosts が存在しないのでスキップ
        result2 = _resolve_note_path(notes_dir, r"..\..\..\Windows\System32\drivers\etc\hosts")
        ok2 = result2 is None
        status2 = "OK" if ok2 else f"FAIL (got={result2})"
        print(f"  [{status2}] 攻撃系1: Windows パストラバーサル → None（スキップ）")
        all_ok = ok2 and all_ok

        # --- 攻撃系 1b: Unix スタイルのパストラバーサル ---
        result2b = _resolve_note_path(notes_dir, "../../../etc/passwd")
        ok2b = result2b is None
        status2b = "OK" if ok2b else f"FAIL (got={result2b})"
        print(f"  [{status2b}] 攻撃系1b: Unix パストラバーサル → None（スキップ）")
        all_ok = ok2b and all_ok

        # --- 攻撃系 2: 絶対パス ---
        # "C:\Windows\evil.md" → basename 化で "evil.md" になり、notes_dir/evil.md が無いのでスキップ
        result3 = _resolve_note_path(notes_dir, r"C:\Windows\evil.md")
        ok3 = result3 is None
        status3 = "OK" if ok3 else f"FAIL (got={result3})"
        print(f"  [{status3}] 攻撃系2: 絶対パス（C:\\Windows\\evil.md）→ None（スキップ）")
        all_ok = ok3 and all_ok

        # --- 攻撃系 3: notes_dir 外のファイルを直接指定（basename 化で同名ファイルがないケース）---
        # "evil" という名前の notes_dir 外ファイルが存在しても basename だけ取り出して
        # notes_dir/evil が存在しないならスキップ
        evil_dir = Path(tmp) / "evil_dir"
        evil_dir.mkdir()
        evil_file = evil_dir / "evil.md"
        evil_file.write_text("悪意あるファイル", encoding="utf-8")
        result4 = _resolve_note_path(notes_dir, str(evil_file))
        # basename 化で "evil.md" → notes_dir/evil.md が存在しない → None
        ok4 = result4 is None
        status4 = "OK" if ok4 else f"FAIL (got={result4})"
        print(f"  [{status4}] 攻撃系3: notes_dir 外の絶対パス指定 → None（basename 化でスキップ）")
        all_ok = ok4 and all_ok

        # --- 攻撃系 4: シンボリックリンク経由で notes_dir 外を参照しようとするケース ---
        # Windows では管理者権限がないとシンボリックリンク作成が失敗することがあるため、
        # 失敗した場合は SKIP とする
        try:
            outside_file = Path(tmp) / "outside_secret.md"
            outside_file.write_text("秘密のファイル", encoding="utf-8")
            symlink = notes_dir / "symlink_to_outside.md"
            symlink.symlink_to(outside_file)

            result5 = _resolve_note_path(notes_dir, "symlink_to_outside.md")
            # resolve(strict=True) でシンボリックリンクの実体パス（notes_dir 外）に解決される
            # → relative_to(notes_root) が ValueError → None
            ok5 = result5 is None
            status5 = "OK" if ok5 else f"FAIL (got={result5})"
            print(f"  [{status5}] 攻撃系4: シンボリックリンク経由で notes_dir 外参照 → None")
            all_ok = ok5 and all_ok
        except (OSError, NotImplementedError):
            # Windows でシンボリックリンク作成権限がない場合など
            print("  [SKIP] 攻撃系4: シンボリックリンク作成権限なし（環境依存のためスキップ）")

    return all_ok


# ===========================================================================
# P2: race condition 修正の単体テスト
#
# note_viewer.py は tkinter GUI を含むため、GUI を起動せずに _search_request_id の
# ロジックだけを独立してテストする。
# ===========================================================================

def test_race_condition_logic() -> bool:
    """race condition 対策（request_id）の単体テスト。

    NoteViewerApp のインスタンスは tkinter を必要とするため作成しない。
    代わりに、同じロジックをシミュレートして正しく動作するかを確認する。
    """
    print("\n=== P2: race condition 対策テスト ===")
    all_ok = True

    # --- シミュレーション: request_id の仕組みを模倣 ---
    # 検索ごとに request_id を +1 し、古いリクエストの結果は無視する
    class MockSearchState:
        """NoteViewerApp の _search_request_id と _on_semantic_results ロジックを模倣する。"""
        def __init__(self) -> None:
            self._search_request_id: int = 0
            self.applied_results: list[list] = []  # 実際に UI に反映された結果を記録する

        def start_search(self, results: list, delay: float) -> None:
            """検索を発行し、delay 秒後に結果が返ってくることをシミュレートする。"""
            self._search_request_id += 1
            my_id = self._search_request_id

            def _run() -> None:
                time.sleep(delay)
                # メインスレッドへの after() の代わりに直接呼ぶ
                self._on_semantic_results(results, my_id)

            threading.Thread(target=_run, daemon=True).start()

        def _on_semantic_results(self, results: list, request_id: int) -> None:
            """古いリクエストの結果は無視する（本番コードと同じロジック）。"""
            if request_id != self._search_request_id:
                return  # 古いリクエスト → 捨てる
            self.applied_results.append(results)

    # --- ケース1: 1回だけ検索 → 結果が1件反映される ---
    state1 = MockSearchState()
    state1.start_search(["result_A"], delay=0.05)
    time.sleep(0.15)
    ok1 = state1.applied_results == [["result_A"]]
    status1 = "OK" if ok1 else f"FAIL (applied={state1.applied_results})"
    print(f"  [{status1}] 1回だけ検索: 結果が正常に反映される")
    all_ok = ok1 and all_ok

    # --- ケース2: 2回連続検索 → 最後の結果だけ反映される（古い遅い結果は無視）---
    # 1回目: 遅い検索（0.2 秒）→ old_results
    # 2回目: 速い検索（0.05 秒）→ new_results
    # 期待: new_results だけが applied_results に入る
    state2 = MockSearchState()
    state2.start_search(["old_results"], delay=0.2)   # 遅い古いリクエスト
    time.sleep(0.01)  # 少し待ってから新しい検索を発行
    state2.start_search(["new_results"], delay=0.05)  # 速い新しいリクエスト

    # 両方のスレッドが完了するまで待つ
    time.sleep(0.4)

    ok2 = state2.applied_results == [["new_results"]]
    status2 = "OK" if ok2 else f"FAIL (applied={state2.applied_results})"
    print(f"  [{status2}] 遅い古い検索の後に速い新しい検索: 新しい結果だけ反映される")
    all_ok = ok2 and all_ok

    # --- ケース3: 3回連続検索 → 最後の結果だけ反映される ---
    state3 = MockSearchState()
    state3.start_search(["result_1"], delay=0.3)   # 最初の遅いリクエスト
    time.sleep(0.01)
    state3.start_search(["result_2"], delay=0.2)   # 2番目のリクエスト
    time.sleep(0.01)
    state3.start_search(["result_3"], delay=0.05)  # 最後の速いリクエスト

    time.sleep(0.5)

    ok3 = state3.applied_results == [["result_3"]]
    status3 = "OK" if ok3 else f"FAIL (applied={state3.applied_results})"
    print(f"  [{status3}] 3回連続検索: 最後の結果だけ反映される")
    all_ok = ok3 and all_ok

    # --- ケース4: 1回検索して全部終わった後にもう1回 → それぞれ独立して反映される ---
    state4 = MockSearchState()
    state4.start_search(["first"], delay=0.05)
    time.sleep(0.15)  # 1回目が完了するまで待つ
    state4.start_search(["second"], delay=0.05)
    time.sleep(0.15)

    ok4 = state4.applied_results == [["first"], ["second"]]
    status4 = "OK" if ok4 else f"FAIL (applied={state4.applied_results})"
    print(f"  [{status4}] 順番に2回検索: 両方とも独立して反映される")
    all_ok = ok4 and all_ok

    return all_ok


def test_note_viewer_has_request_id() -> bool:
    """note_viewer.py に _search_request_id と request_id チェックが実装されていることを確認する。

    ソースコードを直接読んで期待するコードが存在するかを確認するスモークテスト。
    """
    print("\n=== P2: note_viewer.py の request_id 実装確認テスト ===")
    all_ok = True

    source = Path(__file__).parent / "note_viewer.py"
    code = source.read_text(encoding="utf-8")

    checks = [
        ("_search_request_id: int = 0",  "_search_request_id の初期化が存在する"),
        ("self._search_request_id += 1",  "検索発行時に request_id をインクリメントしている"),
        ("my_id = self._search_request_id", "my_id に現在の request_id を保存している"),
        ("if request_id != self._search_request_id", "古いリクエストを破棄する条件分岐がある"),
        ("def _on_semantic_results(self, result_paths: list[Path], query: str, request_id: int)",
         "_on_semantic_results が request_id 引数を受け取るようになっている"),
    ]

    for snippet, description in checks:
        ok = snippet in code
        status = "OK" if ok else f"FAIL（コード中に見つからない: {repr(snippet[:50])}...）"
        print(f"  [{status}] {description}")
        all_ok = ok and all_ok

    return all_ok


def test_search_lib_has_path_traversal_fix() -> bool:
    """search_lib.py にパストラバーサル対策コードが実装されていることを確認する。

    ソースコードを直接読んで期待するコードが存在するかを確認するスモークテスト。
    """
    print("\n=== P1: search_lib.py のパストラバーサル修正確認テスト ===")
    all_ok = True

    source = Path(__file__).parent / "search_lib.py"
    code = source.read_text(encoding="utf-8")

    checks = [
        ("safe_name = Path(source_file).name",       "basename だけを使う処理が存在する"),
        ("candidate = notes_dir / safe_name",         "basename と notes_dir を結合している"),
        ("resolved = candidate.resolve(strict=True)", "resolve(strict=True) で存在チェックしている"),
        ("notes_root = notes_dir.resolve(strict=True)", "notes_root を resolve して基準パスを取得している"),
        ("resolved.relative_to(notes_root)",          "relative_to() で配下かどうかを検証している"),
        ("except (FileNotFoundError, ValueError):",   "FileNotFoundError と ValueError を捕捉している"),
    ]

    for snippet, description in checks:
        ok = snippet in code
        status = "OK" if ok else f"FAIL（コード中に見つからない: {repr(snippet[:50])}）"
        print(f"  [{status}] {description}")
        all_ok = ok and all_ok

    return all_ok


# ===========================================================================
# エントリポイント
# ===========================================================================

if __name__ == "__main__":
    results = [
        test_path_traversal(),
        test_race_condition_logic(),
        test_note_viewer_has_request_id(),
        test_search_lib_has_path_traversal_fix(),
    ]
    print()
    if all(results):
        print("=== 全テスト PASS ===")
        sys.exit(0)
    else:
        print("=== テスト FAIL あり ===")
        sys.exit(1)
