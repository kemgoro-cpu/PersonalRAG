"""test_phase_b.py
フェーズ B の単体テスト（サニタイズ / 履歴管理 / meta.json / YAMLフロントマター）。

本番モジュールを直接 import してテストする。
  - recording_meta.py: sanitize_title / add_title_to_history / save_meta_json 等
  - summarize.py: load_recording_meta / build_recording_frontmatter / render_markdown

これにより本番コードを変更したときに回帰を検知できる。

実行:
    python scripts/test_phase_b.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import yaml

# scripts/ ディレクトリを sys.path に追加して本番モジュールを import できるようにする
# （他の test_*.py と同じパターン）
sys.path.insert(0, str(Path(__file__).parent))

# 本番モジュールから直接 import する（ロジックを複製しない）
from recording_meta import (
    sanitize_title,
    add_title_to_history,
    load_title_history,
    save_title_history,
    save_meta_json,
)
from summarize import (
    load_recording_meta,
    build_recording_frontmatter,
    render_markdown,
)


# ============================================================
# テスト実行
# ============================================================

def test_sanitize_title() -> bool:
    """sanitize_title の単体テスト。"""
    cases = [
        ("日本語タイトル（変換なし）",  "打ち合わせXYZ",              "打ち合わせXYZ"),
        ("不正文字 / : * ? < > |",      'a/b:c*d?e"f<g>h|i',          "a_b_c_d_e_f_g_h_i"),
        ("改行・タブ",                   "a\nb\tc",                    "a_b_c"),
        ("制御文字",                     "a\x00b\x1fc",                "a_b_c"),
        ("末尾の . と空白",              "test.  . ",                  "test"),
        ("50 文字丁度",                  "あ" * 50,                    "あ" * 50),
        ("51 文字を 50 文字に切る",      "あ" * 51,                    "あ" * 50),
        ("予約名 CON",                   "CON",                        "CON_"),
        ("予約名 com1（小文字）",        "com1",                       "com1_"),
        ("予約名 NUL",                   "NUL",                        "NUL_"),
        ("空文字列",                     "",                           ""),
        ("空白のみ",                     "   ",                        ""),
        ("点のみ",                       "...",                        ""),
        ("英数字とハイフン（変換なし）", "meeting-2026",               "meeting-2026"),
        ("バックスラッシュ",             "a\\b",                       "a_b"),
    ]
    print("=== sanitize_title テスト ===")
    all_ok = True
    for name, inp, expected in cases:
        got = sanitize_title(inp)
        ok = got == expected
        status = "OK" if ok else f"FAIL (got={repr(got)}, expected={repr(expected)})"
        print(f"  [{status}] {name}")
        if not ok:
            all_ok = False
    return all_ok


def test_title_history() -> bool:
    """add_title_to_history の単体テスト。"""
    print("\n=== add_title_to_history テスト ===")
    all_ok = True

    def check(desc: str, got: list[str], expected: list[str]) -> bool:
        ok = got == expected
        status = "OK" if ok else f"FAIL (got={got}, expected={expected})"
        print(f"  [{status}] {desc}")
        return ok

    h = ["b", "c", "d"]
    h = add_title_to_history("a", h)
    all_ok = check("新規追加（先頭に）", h, ["a", "b", "c", "d"]) and all_ok

    h = add_title_to_history("c", h)
    all_ok = check("重複を先頭に移動", h, ["c", "a", "b", "d"]) and all_ok

    h2 = add_title_to_history("", h)
    all_ok = check("空文字は追加しない", h2, h) and all_ok

    h3 = add_title_to_history("   ", h)
    all_ok = check("空白のみも追加しない", h3, h) and all_ok

    h4 = ["1", "2", "3", "4", "5"]
    h4 = add_title_to_history("6", h4)
    ok = len(h4) == 5 and h4[0] == "6"
    status = "OK" if ok else f"FAIL: {h4}"
    print(f"  [{status}] 5 件超えで切り詰め")
    all_ok = ok and all_ok

    return all_ok


def test_meta_json() -> bool:
    """meta.json の保存・読み込みテスト。"""
    print("\n=== meta.json 保存・読み込みテスト ===")
    all_ok = True

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        # 通常保存と内容確認
        wav = tmp_path / "rec_2026-05-15_143022_打ち合わせ.wav"
        save_meta_json(wav, "打ち合わせ", "田中, 佐藤", "設計レビュー")
        meta_p = tmp_path / "rec_2026-05-15_143022_打ち合わせ.meta.json"
        ok = meta_p.exists()
        print(f"  [{'OK' if ok else 'FAIL'}] meta.json が作成される")
        all_ok = ok and all_ok

        if ok:
            data = json.loads(meta_p.read_text(encoding="utf-8"))
            ok2 = (
                data["title"] == "打ち合わせ"
                and data["participants"] == "田中, 佐藤"
                and data["topic"] == "設計レビュー"
                and "recorded_at" in data
            )
            print(f"  [{'OK' if ok2 else 'FAIL'}] 内容確認（title/participants/topic/recorded_at）")
            all_ok = ok2 and all_ok

        # transcript 向けの meta を simulate して load_recording_meta をテスト
        transcript = tmp_path / "rec_xxx_2026-05-15_1430.txt"
        transcript_meta = tmp_path / "rec_xxx_2026-05-15_1430.meta.json"
        transcript_meta.write_text(
            json.dumps({"title": "T", "participants": "P", "topic": "Q", "recorded_at": "R"},
                       ensure_ascii=False),
            encoding="utf-8",
        )
        result = load_recording_meta(transcript)
        ok3 = result == {"title": "T", "participants": "P", "topic": "Q", "recorded_at": "R"}
        print(f"  [{'OK' if ok3 else 'FAIL'}] load_recording_meta 正常読み込み")
        all_ok = ok3 and all_ok

        # ファイルなし → None
        transcript2 = tmp_path / "no_meta.txt"
        transcript2.touch()
        result2 = load_recording_meta(transcript2)
        ok4 = result2 is None
        print(f"  [{'OK' if ok4 else 'FAIL'}] meta.json なし → None")
        all_ok = ok4 and all_ok

        # 壊れた JSON → None
        broken_meta = tmp_path / "broken.meta.json"
        broken_meta.write_text("{broken json", encoding="utf-8")
        broken_transcript = tmp_path / "broken.txt"
        result3 = load_recording_meta(broken_transcript)
        ok5 = result3 is None
        print(f"  [{'OK' if ok5 else 'FAIL'}] 壊れた JSON → None（フォールバック）")
        all_ok = ok5 and all_ok

        # 欠損フィールド → 空文字列で補完
        partial_meta = tmp_path / "partial.meta.json"
        partial_meta.write_text(json.dumps({"title": "Only Title"}), encoding="utf-8")
        partial_transcript = tmp_path / "partial.txt"
        result4 = load_recording_meta(partial_transcript)
        ok6 = result4 == {"title": "Only Title", "participants": "", "topic": "", "recorded_at": ""}
        print(f"  [{'OK' if ok6 else 'FAIL'}] 欠損フィールドは空文字列で補完")
        all_ok = ok6 and all_ok

    return all_ok


def test_frontmatter() -> bool:
    """build_recording_frontmatter の単体テスト。"""
    print("\n=== build_recording_frontmatter テスト ===")
    all_ok = True

    # 通常ケース
    meta = {"title": "打ち合わせ", "participants": "田中", "topic": "設計", "recorded_at": "2026-05-15T14:30:22+09:00"}
    fm = build_recording_frontmatter(meta)
    ok = fm.startswith("---\n") and "title:" in fm
    print(f"  [{'OK' if ok else 'FAIL'}] フロントマター形式が正しい")
    all_ok = ok and all_ok

    # YAML として valid か確認
    inner = fm[4:fm.rfind("---\n")]
    try:
        parsed = yaml.safe_load(inner)
        ok2 = parsed["title"] == "打ち合わせ"
        print(f"  [{'OK' if ok2 else 'FAIL'}] valid YAML かつ title が正しい")
        all_ok = ok2 and all_ok
    except yaml.YAMLError as e:
        print(f"  [FAIL] YAML パースエラー: {e}")
        all_ok = False

    # コロンを含むタイトル（特殊文字）
    meta2 = {"title": "会議: 重要事項について", "participants": "", "topic": "", "recorded_at": ""}
    fm2 = build_recording_frontmatter(meta2)
    inner2 = fm2[4:fm2.rfind("---\n")]
    try:
        parsed2 = yaml.safe_load(inner2)
        ok3 = parsed2["title"] == "会議: 重要事項について"
        print(f"  [{'OK' if ok3 else 'FAIL'}] コロンを含むタイトルも valid YAML")
        all_ok = ok3 and all_ok
    except yaml.YAMLError as e:
        print(f"  [FAIL] コロン入りタイトルの YAML パースエラー: {e}")
        all_ok = False

    # 全フィールドが空 → 空文字列
    meta3 = {"title": "", "participants": "", "topic": "", "recorded_at": ""}
    fm3 = build_recording_frontmatter(meta3)
    ok4 = fm3 == ""
    print(f"  [{'OK' if ok4 else 'FAIL'}] 全フィールドが空 → 空文字列（挿入しない）")
    all_ok = ok4 and all_ok

    # ダブルクォートを含むタイトル
    meta4 = {"title": 'He said "hello"', "participants": "", "topic": "", "recorded_at": ""}
    fm4 = build_recording_frontmatter(meta4)
    inner4 = fm4[4:fm4.rfind("---\n")]
    try:
        parsed4 = yaml.safe_load(inner4)
        ok5 = parsed4["title"] == 'He said "hello"'
        print(f"  [{'OK' if ok5 else 'FAIL'}] ダブルクォートを含むタイトルも valid YAML")
        all_ok = ok5 and all_ok
    except yaml.YAMLError as e:
        print(f"  [FAIL] YAML パースエラー: {e}")
        all_ok = False

    return all_ok


def test_render_markdown_unified_frontmatter() -> bool:
    """render_markdown の統合テスト: フロントマターが1ブロックに統合されること。

    P1 修正の検証テスト。録音メタあり/なし の両ケースで確認する。
    """
    print("\n=== render_markdown フロントマター統合テスト ===")
    all_ok = True

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        # ダミーの transcript パスを用意（ファイルは不要、名前だけ使う）
        transcript = tmp_path / "rec_2026-05-15_143022_打ち合わせ.txt"

        # --- ケース1: 録音メタあり + 既存メタあり → 1ブロックに統合 ---
        recording_meta = {
            "title": "打ち合わせ",
            "participants": "田中",
            "topic": "設計レビュー",
            "recorded_at": "2026-05-15T14:30:22+09:00",
        }
        parsed_result = {
            "summary": "テスト要約",
            "topics": ["トピックA"],
            "decisions": [],
            "todos": [],
            "questions": [],
            "keywords": ["ECU", "設計"],
        }
        md = render_markdown(parsed_result, "", transcript, recording_meta=recording_meta)

        # フロントマターが1ブロックだけであることを確認する
        # text.split("---") で分割して "---" の出現回数を数える
        # 正常: "---\nYAML\n---\n\n本文" → split で 3 パーツ（空, YAML, 本文）
        # バグ: "---\nYAML1\n---\n---\nYAML2\n---\n\n本文" → split で 5 パーツ
        parts = md.split("---")
        # parts[0] が空文字、parts[1] が YAML 本体、parts[2] 以降が本文のはず
        ok1 = len(parts) == 3
        print(f"  [{'OK' if ok1 else 'FAIL'}] 録音メタあり: フロントマターが1ブロック（split('---') == 3 パーツ）")
        if not ok1:
            print(f"    実際の分割数: {len(parts)}, 先頭200文字: {repr(md[:200])}")
        all_ok = ok1 and all_ok

        # フロントマターを YAML パースして全フィールドが読めることを確認
        if ok1:
            yaml_body = parts[1].strip()
            try:
                fm_data = yaml.safe_load(yaml_body)
                required_keys = ["title", "participants", "topic", "recorded_at", "source", "date", "keywords", "generated_at"]
                missing = [k for k in required_keys if k not in fm_data]
                ok2 = len(missing) == 0
                print(f"  [{'OK' if ok2 else 'FAIL'}] 録音メタあり: 全フィールドが YAML パースで読める（不足: {missing}）")
                all_ok = ok2 and all_ok

                # 値の確認（title/participants/topic/recorded_at が正しく入っているか）
                ok3 = (
                    fm_data.get("title") == "打ち合わせ"
                    and fm_data.get("participants") == "田中"
                    and fm_data.get("topic") == "設計レビュー"
                    and fm_data.get("recorded_at") == "2026-05-15T14:30:22+09:00"
                )
                print(f"  [{'OK' if ok3 else 'FAIL'}] 録音メタあり: 録音メタの値が正しく格納されている")
                all_ok = ok3 and all_ok

                # keywords が ingest_db.py で参照できる形（リストとして読める）
                ok4 = isinstance(fm_data.get("keywords"), list) and "ECU" in fm_data["keywords"]
                print(f"  [{'OK' if ok4 else 'FAIL'}] 録音メタあり: keywords がリストとして読める")
                all_ok = ok4 and all_ok

            except yaml.YAMLError as e:
                print(f"  [FAIL] YAML パースエラー: {e}")
                all_ok = False

        # --- ケース2: 録音メタなし → 既存フロントマターのみ（既存回帰なし） ---
        md2 = render_markdown(parsed_result, "", transcript, recording_meta=None)
        parts2 = md2.split("---")
        ok5 = len(parts2) == 3
        print(f"  [{'OK' if ok5 else 'FAIL'}] 録音メタなし: フロントマターが1ブロック（既存動作維持）")
        all_ok = ok5 and all_ok

        if ok5:
            yaml_body2 = parts2[1].strip()
            try:
                fm_data2 = yaml.safe_load(yaml_body2)
                # 録音メタなしの場合は録音メタのキーが含まれないことを確認
                ok6 = (
                    "source" in fm_data2
                    and "date" in fm_data2
                    and "keywords" in fm_data2
                    and "title" not in fm_data2
                    and "participants" not in fm_data2
                )
                print(f"  [{'OK' if ok6 else 'FAIL'}] 録音メタなし: source/date/keywords のみ（録音メタキーは含まれない）")
                all_ok = ok6 and all_ok
            except yaml.YAMLError as e:
                print(f"  [FAIL] YAML パースエラー: {e}")
                all_ok = False

        # --- ケース3: パース失敗（parsed=None）でも1ブロックになる ---
        md3 = render_markdown(None, "raw LLM output", transcript, recording_meta=recording_meta)
        parts3 = md3.split("---")
        ok7 = len(parts3) == 3
        print(f"  [{'OK' if ok7 else 'FAIL'}] JSON パース失敗ケース: フロントマターが1ブロック")
        all_ok = ok7 and all_ok

    return all_ok


if __name__ == "__main__":
    results = [
        test_sanitize_title(),
        test_title_history(),
        test_meta_json(),
        test_frontmatter(),
        test_render_markdown_unified_frontmatter(),
    ]
    print()
    if all(results):
        print("=== 全テスト PASS ===")
        sys.exit(0)
    else:
        print("=== テスト FAIL あり ===")
        sys.exit(1)
