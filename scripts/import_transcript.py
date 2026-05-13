"""import_transcript.py
Step 1-alt: 既存のテキスト文字起こしファイルを PersonalRAG に取り込むスクリプト。

音声ファイルを使わず、すでに文字起こしされたテキスト（Teams トランスクリプト、
iPhone ボイスメモの起こし、Zoom .vtt など）を Step 2（要約）以降に流せる形式に
正規化して data/transcripts/ に保存する。

処理の流れ:
    1. ファイルの拡張子で形式を判定（.docx / .vtt / .txt / .md）
    2. 形式ごとのパーサーで本文・話者・タイムスタンプを抽出
    3. transcribe.py の出力フォーマットと同じ形式で .txt に書き出す

使い方:
    python scripts/import_transcript.py <テキストファイルパス>

例:
    python scripts/import_transcript.py data/input_text/teams_meeting.docx
    python scripts/import_transcript.py data/input_text/iphone_memo.txt
    python scripts/import_transcript.py data/input_text/zoom.vtt
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from config_loader import load_settings, resolve_path


# ---------------------------------------------------------------------------
# 文字コード自動判定付きファイル読み込み
# ---------------------------------------------------------------------------

def read_text_with_fallback(path: Path) -> str:
    """複数の文字コードを順番に試してファイルを読み込む。

    Windows の古いファイルは Shift-JIS（cp932）で保存されていることがある。
    utf-8 → utf-8-sig（BOM 付き UTF-8）→ cp932 の順に試し、
    どれでも駄目なら errors='replace' で強制読み込みする。

    Args:
        path: 読み込むファイルのパス。

    Returns:
        ファイルの内容を文字列として返す。
    """
    # 試す順番と失敗時のフォールバック処理を定義
    encodings = ["utf-8", "utf-8-sig", "cp932"]
    for enc in encodings:
        try:
            return path.read_text(encoding=enc)
        except (UnicodeDecodeError, ValueError):
            # この文字コードでは読めなかったので次を試す
            continue

    # すべて失敗した場合は utf-8 で「読めない文字を ? に置き換えて」強制読み込み
    print(f"[warn] 文字コードを特定できません。不明な文字を置換して読み込みます: {path.name}")
    return path.read_text(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# 形式別パーサー
# ---------------------------------------------------------------------------

def parse_docx(path: Path) -> tuple[str, str]:
    """Teams トランスクリプト（.docx）をパースする。

    Teams がエクスポートする .docx は、段落単位で以下のパターンが繰り返される:
        John Smith    0:00:32
        本日の議題ですが、ECU 開発の進捗について確認します。

    話者名と時刻が同じ行にあり、その直後の段落が発言内容になっている。
    パターンが検出できなかった場合はプレーンテキストとして全段落を結合する
    （フォールバック）。

    Args:
        path: .docx ファイルのパス。

    Returns:
        (本文テキスト, source_type) のタプル。
        source_type は "teams"（パターン検出成功）または "plain"（フォールバック）。
    """
    try:
        from docx import Document
    except ImportError:
        # python-docx がインストールされていない場合の案内
        print("[error] python-docx が未インストールです。以下を実行してください:")
        print("        pip install python-docx>=1.1.0")
        raise

    doc = Document(str(path))
    paragraphs = [p.text for p in doc.paragraphs]

    # Teams トランスクリプトのパターン: 「話者名（スペース）時刻」
    # 時刻は 0:32 や 1:23:45 のような形式
    speaker_time_pattern = re.compile(r"^(.+?)\s{2,}(\d+:\d{2}(?::\d{2})?)$")

    lines: list[str] = []
    pattern_found = False  # パターンが1つでも見つかったか

    # ---- インデックスベースのループに変更した理由 ----
    # 複数段落の発言を「次の話者ヘッダーが来るまで」まとめて取得するには、
    # 内側のループで段落を先読みしてから外側のループの位置を更新する必要がある。
    # イテレータ（for ... in）では「先読みした段落を戻す」ことができないため、
    # インデックス i を手動で管理するスタイルの方がコードがシンプルになる。
    i = 0
    while i < len(paragraphs):
        text = paragraphs[i].strip()
        match = speaker_time_pattern.match(text)

        if match:
            pattern_found = True
            speaker = match.group(1).strip()
            time_raw = match.group(2).strip()

            # 時刻を HH:MM:SS 形式に正規化する
            # 「0:32」のような分:秒形式は「00:00:32」に変換
            time_parts = time_raw.split(":")
            if len(time_parts) == 2:
                # 分:秒 → 時:分:秒（時間は 0 として補完）
                time_str = f"00:{int(time_parts[0]):02d}:{int(time_parts[1]):02d}"
            elif len(time_parts) == 3:
                time_str = (
                    f"{int(time_parts[0]):02d}:"
                    f"{int(time_parts[1]):02d}:"
                    f"{int(time_parts[2]):02d}"
                )
            else:
                time_str = "00:00:00"

            # ---- バグ修正: 発言が複数段落に分かれている場合に対応 ----
            # 以前は話者ヘッダー直後の 1 段落しか取得していなかった。
            # Teams の議事録では長い発言が複数の段落に分かれることがあるため、
            # 次の話者ヘッダーが現れるまで（または .docx 末尾まで）すべての
            # 非空段落を連結するよう修正する。
            # 段落間の連結には改行 "\n" を使う（要約 LLM に段落区切りを伝えるため）。
            utterance_parts: list[str] = []
            j = i + 1
            while j < len(paragraphs):
                candidate = paragraphs[j].strip()
                if not candidate:
                    # 空白段落はスキップ（ただし外側ループの位置は進める）
                    j += 1
                    continue
                if speaker_time_pattern.match(candidate):
                    # 次の話者ヘッダーを見つけたらここで停止する。
                    # j を更新しないことで、外側ループが次のイテレーションで
                    # このヘッダーを正しく処理できる。
                    break
                utterance_parts.append(candidate)
                j += 1

            # 内側ループで消費した最後のインデックスまで外側ループを進める
            i = j - 1

            utterance = "\n".join(utterance_parts)
            if utterance:
                # transcribe.py の出力と同じ形式: [HH:MM:SS - HH:MM:SS] 話者名: 発言
                # 終了時刻が不明なので開始時刻を流用（見た目上の影響は最小限）
                lines.append(f"[{time_str} - {time_str}] {speaker}: {utterance}")
        i += 1

    if pattern_found and lines:
        return "\n".join(lines), "teams"

    # パターンが見つからなかった場合: 全段落をプレーンテキストとして結合（フォールバック）
    print(f"[warn] Teams パターンを検出できませんでした。プレーンテキストとして処理します: {path.name}")
    plain_text = "\n".join(p for p in paragraphs if p.strip())
    return plain_text, "plain"


def parse_vtt(path: Path) -> tuple[str, str]:
    """WebVTT（.vtt）形式をパースする。

    WebVTT の基本構造:
        WEBVTT

        00:00:00.000 --> 00:00:05.500
        <v 話者名>本文

    空行でブロックを分割し、各ブロックからタイムスタンプと話者を抽出する。
    話者タグ（<v 名前>）が無い場合は UNKNOWN として扱う。

    Args:
        path: .vtt ファイルのパス。

    Returns:
        (本文テキスト, "zoom") のタプル。
    """
    content = read_text_with_fallback(path)

    # ---- 改行コード正規化 ----
    # Windows で生成された .vtt は行末が CRLF（\r\n）になっており、
    # そのまま split("\n\n") すると \r が残って分割できない。
    # read_text_with_fallback は encoding を指定しているが newline= を指定していないため
    # Python のユニバーサル改行モードが働かず \r\n がそのまま渡される場合がある。
    # ここで明示的に CRLF / CR のいずれも \n に統一しておく。
    content = content.replace("\r\n", "\n").replace("\r", "\n")

    # 空行（\n が 2 つ以上連続する箇所）でブロックを分割する（VTT の基本構造）
    # re.split を使うことで、空行に余分なスペースが混入していても安全に分割できる
    blocks = re.split(r'\n\s*\n', content.strip())

    # タイムスタンプ行のパターン: 00:00:00.000 --> 00:00:05.500
    # 時間フォーマットは HH:MM:SS.mmm または MM:SS.mmm
    timestamp_pattern = re.compile(
        r"(\d{1,2}:\d{2}:\d{2}|\d{2}:\d{2})\.\d+ --> (\d{1,2}:\d{2}:\d{2}|\d{2}:\d{2})\.\d+"
    )
    # 話者タグのパターン: <v John Smith> または <v John Smith>テキスト
    speaker_tag_pattern = re.compile(r"<v\s+([^>]+)>(.*)$", re.DOTALL)

    lines: list[str] = []

    for block in blocks:
        block_lines = block.strip().splitlines()
        if not block_lines:
            continue

        # 先頭行が "WEBVTT" なら全体ヘッダーなのでスキップ
        if block_lines[0].strip().upper().startswith("WEBVTT"):
            continue

        # タイムスタンプ行を探す
        start_time = ""
        end_time = ""
        text_lines: list[str] = []
        found_timestamp = False

        for line in block_lines:
            ts_match = timestamp_pattern.match(line.strip())
            if ts_match and not found_timestamp:
                found_timestamp = True
                # タイムスタンプを秒部分だけに整形（ミリ秒は不要）
                start_time = ts_match.group(1)
                end_time = ts_match.group(2)
                # MM:SS 形式を HH:MM:SS に変換
                if start_time.count(":") == 1:
                    start_time = f"00:{start_time}"
                if end_time.count(":") == 1:
                    end_time = f"00:{end_time}"
            elif found_timestamp:
                # タイムスタンプ以降の行が本文
                text_lines.append(line)

        if not found_timestamp or not text_lines:
            continue

        # 本文から話者タグ（<v 名前>）を抽出する
        full_text = " ".join(text_lines).strip()
        speaker_match = speaker_tag_pattern.match(full_text)

        if speaker_match:
            speaker = speaker_match.group(1).strip()
            utterance = speaker_match.group(2).strip()
        else:
            # 話者タグが無いので UNKNOWN として扱う
            speaker = "UNKNOWN"
            # <v> 以外のタグ（<c> など）を除去して本文を取得
            utterance = re.sub(r"<[^>]+>", "", full_text).strip()

        if utterance:
            lines.append(f"[{start_time} - {end_time}] {speaker}: {utterance}")

    return "\n".join(lines), "zoom"


def parse_plain_text(path: Path) -> tuple[str, str]:
    """プレーンテキスト（.txt）または Markdown（.md）をパースする。

    特別なパースは行わず、メタヘッダーを付けるだけ。
    拡張子で source_type を判定する。

    Args:
        path: テキストファイルのパス。

    Returns:
        (本文テキスト, source_type) のタプル。
        .txt なら source_type="iphone"、.md なら source_type="plain"。
    """
    content = read_text_with_fallback(path)

    # 拡張子に基づいて想定ソースを判定
    # .txt は iPhone ボイスメモ起こし等を想定
    # .md は Markdown メモを想定
    if path.suffix.lower() == ".md":
        source_type = "plain"
    else:
        source_type = "iphone"

    return content, source_type


# ---------------------------------------------------------------------------
# 出力ファイル名の生成
# ---------------------------------------------------------------------------

def build_output_path(source_path: Path, transcripts_dir: Path) -> Path:
    """出力ファイル名を生成する。

    transcribe.py の build_output_path と同じ命名規則を使う。
    例: teams_meeting.docx → teams_meeting_2026-05-14_1234.txt

    Args:
        source_path: 元のテキストファイルのパス。
        transcripts_dir: 出力先ディレクトリ。

    Returns:
        出力ファイルの Path オブジェクト。
    """
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    return transcripts_dir / f"{source_path.stem}_{timestamp}.txt"


# ---------------------------------------------------------------------------
# 出力ファイルの書き出し
# ---------------------------------------------------------------------------

def write_transcript(
    output_path: Path,
    body: str,
    source_path: Path,
    source_type: str,
) -> None:
    """正規化したテキストを transcribe.py 互換フォーマットで書き出す。

    出力フォーマット（transcribe.py の write_transcript と互換）:
        # 文字起こし結果
        # source: teams_meeting.docx
        # imported_at: 2026-05-14 12:34:56
        # source_type: teams

        [00:00:00 - 00:00:05] John Smith: 本日の議題ですが...

    Args:
        output_path: 出力先ファイルのパス。
        body: パース済みの本文テキスト。
        source_path: 元ファイルのパス（メタ情報として記録）。
        source_type: ファイルの種別（"teams" / "zoom" / "iphone" / "plain"）。
    """
    # メタヘッダーの組み立て
    # transcribe.py は "generated_at" を使うが、今回はインポートなので "imported_at" にする
    header_lines = [
        "# 文字起こし結果",
        f"# source: {source_path.name}",
        f"# imported_at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"# source_type: {source_type}",
        "",
    ]

    content = "\n".join(header_lines) + body + "\n"
    output_path.write_text(content, encoding="utf-8")
    print(f"[output] 保存しました: {output_path}")


# ---------------------------------------------------------------------------
# メイン処理
# ---------------------------------------------------------------------------

def main() -> int:
    """エントリポイント。"""
    parser = argparse.ArgumentParser(
        description="既存テキスト文字起こしを PersonalRAG 用フォーマットに変換して保存する"
    )
    parser.add_argument(
        "text_file",
        type=str,
        help="入力テキストファイルのパス（.txt / .vtt / .docx / .md）",
    )
    args = parser.parse_args()

    text_path = Path(args.text_file).resolve()
    if not text_path.exists():
        print(f"[error] ファイルが見つかりません: {text_path}", file=sys.stderr)
        return 1

    # 設定ファイルから出力先パスを取得する
    settings = load_settings()
    transcripts_dir = resolve_path(settings["paths"]["transcripts_dir"])
    transcripts_dir.mkdir(parents=True, exist_ok=True)

    ext = text_path.suffix.lower()
    print(f"[step1] テキスト取り込み開始: {text_path.name} (拡張子: {ext})")

    # 拡張子でパーサーを振り分ける
    try:
        if ext == ".docx":
            print("[step1] Teams .docx パーサーで処理します")
            body, source_type = parse_docx(text_path)

        elif ext == ".vtt":
            print("[step1] WebVTT パーサーで処理します")
            body, source_type = parse_vtt(text_path)

        elif ext in (".txt", ".md"):
            print("[step1] プレーンテキスト / Markdown パーサーで処理します")
            body, source_type = parse_plain_text(text_path)

        else:
            # 設定の text_extensions に入っているはずだが、念のため対処
            print(f"[warn] 対応していない拡張子です（{ext}）。プレーンテキストとして処理します")
            body, source_type = parse_plain_text(text_path)

    except Exception as e:
        # パース失敗時もプレーンテキストフォールバックで処理を継続する
        # （1 ファイルの失敗でパイプライン全体を止めない設計）
        print(f"[warn] パース中にエラーが発生しました。プレーンテキストとして処理します: {e}")
        try:
            body = read_text_with_fallback(text_path)
            source_type = "plain"
        except Exception as e2:
            print(f"[error] フォールバック読み込みも失敗しました: {e2}", file=sys.stderr)
            return 1

    # 本文が空だった場合は警告を出しつつ続行（要約スクリプトが空対応するため）
    if not body.strip():
        print("[warn] パース結果が空でした。ファイル内容を確認してください。")

    output_path = build_output_path(text_path, transcripts_dir)
    write_transcript(output_path, body, text_path, source_type)

    print(f"[step1] 完了: source_type={source_type}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
