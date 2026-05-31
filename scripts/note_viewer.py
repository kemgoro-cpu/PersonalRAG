"""note_viewer.py
PersonalRAG の簡易ノートビューア。

Open WebUI を起動せずに data/notes/*.md を一覧・プレビュー・検索できる
軽量な tkinter アプリ。

主な機能:
    - ノート一覧表示（更新日時降順、フロントマターの title を表示）
    - ノート選択 → 右ペインにメタ情報 + 本文プレビュー
    - テキスト検索（全ファイルを開いて部分一致、大文字小文字無視）
    - セマンティック検索（ChromaDB、初回選択時に遅延 import、経過秒数表示つき）
    - 「エディタで開く」ボタンで既定アプリを起動
    - 「更新」ボタンでノート一覧を再読み込み
    - Markdown 見出し・箇条書き・太字の簡易整形プレビュー（外部ライブラリ不要）
    - 「↻ 要約を作り直す」ボタンで元 transcript から再要約
    - 「編集」ボタンでノートを直接編集・アトミック保存
    - 検索履歴（直近10件、プルダウン Combobox）
    - 参加者・テーマ・日付による絞り込みフィルタ

注意事項:
    - Windows 専用（「エディタで開く」が os.startfile を使用）
    - セマンティック検索は ChromaDB + Ollama が起動済みの場合のみ動作
    - ノートは通常数 KB〜数十 KB のため、ファイル読み込みは同期処理で実施

使い方:
    python scripts/note_viewer.py
    または scripts/note_viewer.cmd をダブルクリック
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import tkinter as tk
from tkinter import messagebox, ttk

# ===== ロガー設定 =====
# pythonw.exe 起動時は stdout が存在しないため、ログは NullHandler に流す。
# 将来的にファイルログに切り替えたい場合はここを変更する。
logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

# ===== sys.path に scripts/ ディレクトリを追加（直接実行・import 両方に対応）=====
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from config_loader import load_settings, resolve_path, PROJECT_ROOT

# 検索履歴を保存するファイルパス（record_gui の .gui_history.json と同じ場所）
_HISTORY_FILE = PROJECT_ROOT / "data" / ".gui_history.json"
# 検索履歴のキー名（他のアプリの履歴と衝突しないよう専用キーを使う）
_HISTORY_KEY = "note_search_history"
# 保持する検索履歴の最大件数
_HISTORY_MAX = 10


# ===========================================================================
# フロントマター解析ユーティリティ
# ===========================================================================

def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Markdown テキストから YAML フロントマターを分離・パースする。

    フロントマターが無いノート（フェーズ B 以前に作成されたもの）も
    問題なく処理できる（空辞書と全文を返す）。

    Args:
        text: Markdown 全文。

    Returns:
        (フロントマター辞書, 本文文字列) のタプル。
        フロントマターが無い場合は ({}, text) を返す。
        YAML パース失敗の場合は ({}, text) を返し、警告ログを出す。
    """
    if not text.startswith("---"):
        return {}, text

    # "---" で囲まれたブロックを抽出する
    parts = text.split("---", 2)
    if len(parts) < 3:
        # 閉じ "---" が無いのでフロントマターなしとみなす
        return {}, text

    yaml_block = parts[1]
    body = parts[2].lstrip("\n")

    try:
        import yaml
        meta = yaml.safe_load(yaml_block) or {}
        if not isinstance(meta, dict):
            # YAML が dict 以外（リスト等）に解釈された場合
            return {}, text
        return meta, body
    except Exception as exc:
        logger.warning(f"フロントマターの YAML パース失敗: {exc}")
        return {}, text


def extract_display_label(md_path: Path, mtime: datetime) -> tuple[str, dict[str, Any]]:
    """ノートファイルを読んで Listbox 表示用ラベルとフロントマターを返す。

    Args:
        md_path: Markdown ファイルのパス。
        mtime: ファイルの更新日時（ソート・表示に使用）。

    Returns:
        (表示ラベル文字列, フロントマター辞書) のタプル。
        ファイル読み込み失敗時は (日付 + ファイル名, {}) を返す。
    """
    date_str = mtime.strftime("%Y-%m-%d")
    try:
        text = md_path.read_text(encoding="utf-8", errors="replace")
        meta, _ = parse_frontmatter(text)
        title = meta.get("title", "")
        if title and str(title).strip():
            # title が存在して空でなければそれを使う
            label = f"{date_str}  {str(title).strip()}"
        else:
            # title なし → ファイル名（拡張子なし）を使う
            label = f"{date_str}  {md_path.stem}"
        return label, meta
    except Exception as exc:
        logger.warning(f"ノートファイルの読み込み失敗 ({md_path.name}): {exc}")
        # 壊れたファイルも表示から除外せず、ファイル名フォールバックで残す
        return f"{date_str}  {md_path.stem}", {}


# ===========================================================================
# テキスト検索ユーティリティ
# ===========================================================================

def search_text(
    query: str,
    note_paths: list[Path],
) -> list[Path]:
    """全ノートファイルを開いて query に部分一致するものを返す。

    大文字小文字は無視する。フロントマター込みの全文を対象にする。

    Args:
        query: 検索文字列。空文字の場合は全件を返す。
        note_paths: 検索対象のファイルパスリスト。

    Returns:
        ヒットしたファイルパスのリスト（note_paths の順序を維持）。
    """
    if not query.strip():
        return list(note_paths)

    query_lower = query.lower()
    matched: list[Path] = []
    for path in note_paths:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            if query_lower in text.lower():
                matched.append(path)
        except Exception as exc:
            logger.warning(f"テキスト検索中にファイル読み込み失敗 ({path.name}): {exc}")
    return matched


# ===========================================================================
# Markdown 簡易整形ユーティリティ（外部ライブラリ不要）
# ===========================================================================

def apply_markdown_tags(widget: tk.Text, body: str, font_size: int = 10) -> None:
    """Markdown 本文を tk.Text ウィジェットに挿入しながら簡易整形する。

    外部ライブラリは使わず、tk.Text のタグ機能だけで整形する。
    対応する書式:
        - 見出し (# 〜 ######): 太字＋大きめフォント（# 記号は除去）
        - 箇条書き (- / * で始まる行): 行頭を「・」に置換
        - 太字 (**xxx**): 太字タグ適用（** 記号は除去）

    Args:
        widget: 書き込む tk.Text ウィジェット（state="normal" の状態で呼ぶこと）。
        body:   Markdown 本文（フロントマター除去済み）。
        font_size: ベースフォントサイズ（settings の ui.font_size から取得する）。
    """
    # --- テキストタグを定義する ---
    # 見出しレベルに応じてフォントサイズを変える（h1 は大きく、h6 は本文と同じ）
    heading_sizes = {
        1: font_size + 6,  # # → 一番大きく
        2: font_size + 4,  # ##
        3: font_size + 2,  # ###
        4: font_size + 1,  # ####
        5: font_size,      # #####
        6: font_size,      # ######
    }
    for level, size in heading_sizes.items():
        widget.tag_configure(
            f"h{level}",
            font=("", size, "bold"),
        )

    # 太字タグ（**xxx** 向け）
    widget.tag_configure("bold", font=("", font_size, "bold"))

    # --- 行ごとに処理する ---
    # 見出しと箇条書きは行単位の判定、太字はインラインなので行内でさらに処理する
    lines = body.split("\n")
    for i, line in enumerate(lines):
        # 最後の行以外は改行を付ける
        newline = "\n" if i < len(lines) - 1 else ""

        # --- 見出し行の判定 ---
        # 行頭の # 連続数でレベルを決める（# の後にスペースが必要）
        heading_match = re.match(r"^(#{1,6})\s+(.*)", line)
        if heading_match:
            level = len(heading_match.group(1))
            content = heading_match.group(2)
            # 見出し内にも **太字** がある場合はさらに処理（見出し+太字の複合は稀なので単純に挿入）
            _insert_with_bold(widget, content + newline, f"h{level}", font_size)
            continue

        # --- 箇条書き行の判定 ---
        # 行頭が "- " または "* " で始まる行（インデントありも対応）
        bullet_match = re.match(r"^(\s*)[-*]\s+(.*)", line)
        if bullet_match:
            indent = bullet_match.group(1)
            content = bullet_match.group(2)
            # 行頭の "-" を "・" に置き換える（インデントは保持）
            widget.insert("end", indent + "・")
            # 箇条書き内の **太字** も整形する
            _insert_with_bold(widget, content + newline, None, font_size)
            continue

        # --- 通常行（太字のみインライン処理）---
        _insert_with_bold(widget, line + newline, None, font_size)


def _insert_with_bold(
    widget: tk.Text,
    text: str,
    extra_tag: str | None,
    font_size: int,
) -> None:
    """テキストを太字(**xxx**)を認識しながら tk.Text に挿入するヘルパー。

    ** で囲まれた部分は "bold" タグを適用する。
    extra_tag が指定された場合は全挿入テキストにそのタグも適用する（見出し用）。

    Args:
        widget:    挿入先の tk.Text ウィジェット。
        text:      挿入する文字列（改行含む場合あり）。
        extra_tag: 追加で適用するタグ名（見出しタグ等）。None なら追加なし。
        font_size: ベースフォントサイズ（未使用だが将来の拡張用に残す）。
    """
    # **xxx** パターンで分割する（正規表現で ** の外と内を交互に取り出す）
    parts = re.split(r"\*\*(.+?)\*\*", text)
    for j, part in enumerate(parts):
        if not part:
            continue
        tags: list[str] = []
        if extra_tag:
            tags.append(extra_tag)
        # j が奇数のとき = ** で囲まれた部分（太字）
        if j % 2 == 1:
            tags.append("bold")
        widget.insert("end", part, tuple(tags))


# ===========================================================================
# 検索履歴ユーティリティ
# ===========================================================================

def load_search_history() -> list[str]:
    """検索履歴を .gui_history.json から読み込む。

    ファイルが存在しない・読み込み失敗の場合は空リストを返す。

    Returns:
        検索クエリのリスト（新しいものが先頭）。
    """
    try:
        if not _HISTORY_FILE.exists():
            return []
        data = json.loads(_HISTORY_FILE.read_text(encoding="utf-8"))
        return list(data.get(_HISTORY_KEY, []))
    except Exception as exc:
        logger.warning(f"検索履歴の読み込み失敗: {exc}")
        return []


def save_search_history(history: list[str]) -> None:
    """検索履歴を .gui_history.json に保存する。

    他のアプリ（record_gui 等）の履歴と共存できるよう、
    ファイル全体を上書きせず、自分のキーだけを更新する。

    Args:
        history: 保存する検索クエリリスト（最大 _HISTORY_MAX 件）。
    """
    try:
        # 既存ファイルの内容を読んで自分のキーだけ上書きする
        existing: dict[str, Any] = {}
        if _HISTORY_FILE.exists():
            try:
                existing = json.loads(_HISTORY_FILE.read_text(encoding="utf-8"))
            except Exception:
                pass  # 読み込み失敗時は空の dict から始める

        existing[_HISTORY_KEY] = history[:_HISTORY_MAX]

        # アトミック書き込み: 一時ファイルに書いてから replace する
        tmp = _HISTORY_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(_HISTORY_FILE)
    except Exception as exc:
        logger.warning(f"検索履歴の保存失敗: {exc}")


def add_to_search_history(query: str, history: list[str]) -> list[str]:
    """クエリを検索履歴の先頭に追加して重複を除去した新リストを返す。

    すでに同じクエリが履歴にある場合は一度削除してから先頭に追加する（最新化）。

    Args:
        query:   追加するクエリ文字列。
        history: 現在の検索履歴リスト。

    Returns:
        更新後の検索履歴リスト（最大 _HISTORY_MAX 件）。
    """
    # 同一クエリを取り除いてから先頭に追加する（重複排除）
    new_history = [q for q in history if q != query]
    new_history.insert(0, query)
    return new_history[:_HISTORY_MAX]


# ===========================================================================
# メインアプリケーションクラス
# ===========================================================================

class NoteViewerApp:
    """PersonalRAG ノートビューア のメインアプリ。"""

    def __init__(self) -> None:
        # --- 設定読み込み ---
        try:
            settings = load_settings()
            self._notes_dir: Path = resolve_path(settings["paths"]["notes_dir"])
        except Exception as exc:
            # 設定ファイルが無い環境でも起動できるようにフォールバック
            logger.warning(f"設定読み込み失敗（デフォルトパスを使用）: {exc}")
            self._notes_dir = PROJECT_ROOT / "data" / "notes"
        self._settings: dict[str, Any] | None = None
        try:
            self._settings = load_settings()
        except Exception:
            pass

        # --- settings から transcripts_dir を取得（要約再生成に使う）---
        try:
            self._transcripts_dir: Path = resolve_path(
                (self._settings or {}).get("paths", {}).get("transcripts_dir", "data/transcripts")
            )
        except Exception:
            self._transcripts_dir = PROJECT_ROOT / "data" / "transcripts"

        # --- settings から UI フォントサイズを取得（キーが無くても動く）---
        # settings.get("ui", {}).get("font_size", 10) で安全に取得する
        self._font_size: int = int(
            (self._settings or {}).get("ui", {}).get("font_size", 10)
        )

        # --- 状態変数 ---
        # 現在表示中のノートパスリスト（Listbox と 1:1 で対応）
        self._current_paths: list[Path] = []
        # 全ノートパスリスト（検索前の全件リスト、「更新」ボタンでリフレッシュ）
        self._all_paths: list[Path] = []
        # セマンティック検索の結果を格納（ファイルパスのリスト）
        self._semantic_paths: list[Path] = []

        # セマンティック検索モジュールの遅延ロード状態
        # False = まだ import していない / True = import 済み（成功・失敗問わず）
        self._semantic_loaded: bool = False
        # セマンティック検索が利用可能かどうか
        self._semantic_available: bool = False

        # --- race condition 対策 ---
        # 連続して検索を発行したとき、古い遅いレスポンスが新しい結果を上書きしないようにする。
        # 検索を発行するたびに +1 し、完了コールバック側で現在の ID と一致するかを確認する。
        # ID が一致しない（＝より新しい検索が発行済み）なら結果を捨てる。
        self._search_request_id: int = 0

        # --- セマンティック検索の経過秒数カウンター制御 ---
        # カウントアップ中かどうかを示すフラグ。
        # False にすると root.after で予約された次のカウントアップが止まる。
        self._search_timer_active: bool = False
        # 検索開始時刻（カウントアップ表示用）
        self._search_start_time: float = 0.0

        # --- 現在表示中のノートパス ---
        self._current_note_path: Path | None = None
        # 編集モード中かどうかを示すフラグ
        self._edit_mode: bool = False

        # --- 検索履歴 ---
        self._search_history: list[str] = load_search_history()

        # --- フィルタ状態 ---
        # 参加者・テーマフィルタ用の選択値
        self._filter_participant: tk.StringVar
        self._filter_topic: tk.StringVar
        # 日付フィルタ用の選択値（"all" / "today" / "week"）
        self._filter_date: tk.StringVar

        # --- GUI 構築 ---
        self.root = tk.Tk()
        self.root.title("PersonalRAG ノートビューア")
        self.root.geometry("1100x750")
        self.root.resizable(True, True)
        self._build_window()

        # 起動時にノート一覧を読み込む
        self._load_notes()

    # ------------------------------------------------------------------
    # GUI 構築
    # ------------------------------------------------------------------

    def _build_window(self) -> None:
        """ウィンドウ全体のレイアウトを構築する。

        左ペイン（1/3）: 検索バー + 検索モード + フィルタ + ノート一覧 + 操作ボタン
        右ペイン（2/3）: メタ情報ラベル + 本文テキストウィジェット + 編集ボタン
        """
        # --- ルートの grid 設定 ---
        self.root.columnconfigure(0, weight=1)
        self.root.columnconfigure(1, weight=2)
        self.root.rowconfigure(0, weight=1)

        # --- 左ペイン ---
        left_frame = ttk.Frame(self.root, padding=8)
        left_frame.grid(row=0, column=0, sticky="nsew")
        left_frame.rowconfigure(4, weight=1)  # Listbox を伸縮させる行（フィルタ行を追加したので+1）
        left_frame.columnconfigure(0, weight=1)

        self._build_left_pane(left_frame)

        # --- 区切り線 ---
        ttk.Separator(self.root, orient="vertical").grid(
            row=0, column=0, sticky="nes", padx=(0, 0)
        )

        # --- 右ペイン ---
        right_frame = ttk.Frame(self.root, padding=8)
        right_frame.grid(row=0, column=1, sticky="nsew")
        right_frame.rowconfigure(1, weight=1)  # Textウィジェットを伸縮させる行
        right_frame.columnconfigure(0, weight=1)

        self._build_right_pane(right_frame)

    def _build_left_pane(self, frame: ttk.Frame) -> None:
        """左ペインのウィジェットを配置する。"""
        # --- 検索バー（Combobox で履歴プルダウン対応）---
        search_frame = ttk.Frame(frame)
        search_frame.grid(row=0, column=0, sticky="we", pady=(0, 4))
        search_frame.columnconfigure(0, weight=1)

        self._search_var = tk.StringVar()
        # Combobox: 直近10件の検索履歴をプルダウンで選べる
        self._search_combo = ttk.Combobox(
            search_frame,
            textvariable=self._search_var,
            values=self._search_history,
            font=("", self._font_size),
        )
        self._search_combo.grid(row=0, column=0, sticky="we")
        # Enter キーで検索実行
        self._search_combo.bind("<Return>", lambda e: self._on_search())
        # プルダウンから選択した場合も検索実行
        self._search_combo.bind("<<ComboboxSelected>>", lambda e: self._on_search())

        ttk.Button(
            search_frame, text="検索", command=self._on_search, width=6
        ).grid(row=0, column=1, padx=(4, 0))

        # --- 検索モード（ラジオボタン）---
        mode_frame = ttk.Frame(frame)
        mode_frame.grid(row=1, column=0, sticky="we", pady=(0, 6))

        self._search_mode = tk.StringVar(value="text")
        ttk.Radiobutton(
            mode_frame,
            text="テキスト",
            variable=self._search_mode,
            value="text",
        ).pack(side="left")
        ttk.Radiobutton(
            mode_frame,
            text="セマンティック (ChromaDB)",
            variable=self._search_mode,
            value="semantic",
        ).pack(side="left", padx=(8, 0))

        # --- 絞り込みフィルタ ---
        self._build_filter_pane(frame, row=2)

        # --- ノート一覧ラベル ---
        ttk.Label(frame, text="ノート一覧", font=("", 9, "bold")).grid(
            row=3, column=0, sticky="w", pady=(0, 2)
        )

        # --- Listbox + スクロールバー ---
        list_frame = ttk.Frame(frame)
        list_frame.grid(row=4, column=0, sticky="nsew")
        list_frame.rowconfigure(0, weight=1)
        list_frame.columnconfigure(0, weight=1)

        self._listbox = tk.Listbox(
            list_frame,
            selectmode="single",
            font=("", self._font_size),
            activestyle="none",
        )
        self._listbox.grid(row=0, column=0, sticky="nsew")
        self._listbox.bind("<<ListboxSelect>>", self._on_note_select)

        scrollbar = ttk.Scrollbar(
            list_frame, orient="vertical", command=self._listbox.yview
        )
        scrollbar.grid(row=0, column=1, sticky="ns")
        self._listbox.configure(yscrollcommand=scrollbar.set)

        # --- 操作ボタン ---
        btn_frame = ttk.Frame(frame)
        btn_frame.grid(row=5, column=0, sticky="we", pady=(6, 0))

        ttk.Button(
            btn_frame, text="更新", command=self._load_notes, width=8
        ).pack(side="left")
        ttk.Button(
            btn_frame, text="エディタで開く", command=self._open_in_editor, width=14
        ).pack(side="left", padx=(8, 0))

    def _build_filter_pane(self, parent: ttk.Frame, row: int) -> None:
        """絞り込みフィルタUIを構築する。

        参加者・テーマのプルダウンと日付ボタン（全期間/今日/今週）を配置する。
        フィルタ変更時は _apply_filter() を呼んで一覧を絞り込む。

        Args:
            parent: 配置先の親フレーム。
            row:    grid の行番号。
        """
        filter_frame = ttk.LabelFrame(parent, text="絞り込み", padding=(4, 2))
        filter_frame.grid(row=row, column=0, sticky="we", pady=(0, 4))
        filter_frame.columnconfigure(1, weight=1)
        filter_frame.columnconfigure(3, weight=1)

        # --- 参加者フィルタ ---
        ttk.Label(filter_frame, text="参加者:", width=6).grid(row=0, column=0, sticky="e")
        self._filter_participant = tk.StringVar(value="すべて")
        self._filter_participant_combo = ttk.Combobox(
            filter_frame,
            textvariable=self._filter_participant,
            values=["すべて"],
            state="readonly",
            width=12,
        )
        self._filter_participant_combo.grid(row=0, column=1, sticky="we", padx=(2, 6))
        self._filter_participant_combo.bind("<<ComboboxSelected>>", lambda e: self._apply_filter())

        # --- テーマフィルタ ---
        ttk.Label(filter_frame, text="テーマ:", width=6).grid(row=0, column=2, sticky="e")
        self._filter_topic = tk.StringVar(value="すべて")
        self._filter_topic_combo = ttk.Combobox(
            filter_frame,
            textvariable=self._filter_topic,
            values=["すべて"],
            state="readonly",
            width=12,
        )
        self._filter_topic_combo.grid(row=0, column=3, sticky="we", padx=(2, 0))
        self._filter_topic_combo.bind("<<ComboboxSelected>>", lambda e: self._apply_filter())

        # --- 日付フィルタ（ボタングループ）---
        date_frame = ttk.Frame(filter_frame)
        date_frame.grid(row=1, column=0, columnspan=4, sticky="we", pady=(4, 0))

        self._filter_date = tk.StringVar(value="all")
        for val, label in [("all", "全期間"), ("today", "今日"), ("week", "今週")]:
            ttk.Radiobutton(
                date_frame,
                text=label,
                variable=self._filter_date,
                value=val,
                command=self._apply_filter,
            ).pack(side="left", padx=(0, 8))

        # フィルタをリセットするボタン
        ttk.Button(
            date_frame,
            text="リセット",
            command=self._reset_filter,
            width=8,
        ).pack(side="right")

    def _build_right_pane(self, frame: ttk.Frame) -> None:
        """右ペインのウィジェットを配置する。"""
        # --- メタ情報エリア ---
        meta_frame = ttk.LabelFrame(frame, text="メタ情報", padding=(8, 4))
        meta_frame.grid(row=0, column=0, sticky="we", pady=(0, 6))
        meta_frame.columnconfigure(1, weight=1)

        # 表示するメタキーと表示名の定義（フロントマターのキー名と対応）
        META_FIELDS = [
            ("title",       "タイトル"),
            ("participants","参加者"),
            ("topic",       "テーマ"),
            ("recorded_at", "録音日時"),
            ("date",        "日付"),
        ]

        # メタ情報の StringVar と Label を辞書で保持する
        self._meta_vars: dict[str, tk.StringVar] = {}
        for row_idx, (key, label) in enumerate(META_FIELDS):
            ttk.Label(meta_frame, text=f"{label}:", foreground="#666", width=9, anchor="e").grid(
                row=row_idx, column=0, sticky="e", pady=1
            )
            var = tk.StringVar(value="—")
            ttk.Label(meta_frame, textvariable=var, anchor="w").grid(
                row=row_idx, column=1, sticky="w", padx=(6, 0), pady=1
            )
            self._meta_vars[key] = var

        # --- 要約再生成・編集ボタンエリア ---
        action_frame = ttk.Frame(meta_frame)
        action_frame.grid(
            row=len(META_FIELDS), column=0, columnspan=2, sticky="we", pady=(6, 0)
        )

        # 「↻ 要約を作り直す」ボタン
        self._regenerate_btn = ttk.Button(
            action_frame,
            text="↻ 要約を作り直す",
            command=self._on_regenerate_summary,
            width=18,
        )
        self._regenerate_btn.pack(side="left")

        # 「編集」ボタン
        self._edit_btn = ttk.Button(
            action_frame,
            text="編集",
            command=self._on_edit_toggle,
            width=8,
        )
        self._edit_btn.pack(side="left", padx=(8, 0))

        # 「保存」ボタン（編集モード中のみ有効）
        self._save_btn = ttk.Button(
            action_frame,
            text="保存 (Ctrl+S)",
            command=self._on_save_note,
            width=14,
            state="disabled",
        )
        self._save_btn.pack(side="left", padx=(4, 0))

        # 「キャンセル」ボタン（編集モード中のみ有効）
        self._cancel_btn = ttk.Button(
            action_frame,
            text="キャンセル",
            command=self._on_cancel_edit,
            width=10,
            state="disabled",
        )
        self._cancel_btn.pack(side="left", padx=(4, 0))

        # --- 本文プレビューエリア ---
        text_frame = ttk.Frame(frame)
        text_frame.grid(row=1, column=0, sticky="nsew")
        text_frame.rowconfigure(0, weight=1)
        text_frame.columnconfigure(0, weight=1)

        self._text_widget = tk.Text(
            text_frame,
            wrap="word",
            font=("", self._font_size),
            state="disabled",   # 読み取り専用（コピーは可能）
            relief="flat",
            padx=4,
            pady=4,
        )
        self._text_widget.grid(row=0, column=0, sticky="nsew")

        # Ctrl+S で保存（編集モード中のみ動作する）
        self._text_widget.bind("<Control-s>", lambda e: self._on_save_note())

        v_scrollbar = ttk.Scrollbar(
            text_frame, orient="vertical", command=self._text_widget.yview
        )
        v_scrollbar.grid(row=0, column=1, sticky="ns")
        self._text_widget.configure(yscrollcommand=v_scrollbar.set)

        h_scrollbar = ttk.Scrollbar(
            text_frame, orient="horizontal", command=self._text_widget.xview
        )
        h_scrollbar.grid(row=1, column=0, sticky="we")
        self._text_widget.configure(xscrollcommand=h_scrollbar.set)

    # ------------------------------------------------------------------
    # ノート一覧の読み込みと表示
    # ------------------------------------------------------------------

    def _load_notes(self) -> None:
        """notes_dir の *.md を列挙して Listbox に表示する。

        - 更新日時降順でソート
        - フロントマターから title を取得してラベルに使用
        - ディレクトリが存在しない / 空の場合は「ノートがありません」を表示
        - 読み込み後にフィルタ用のプルダウン選択肢を更新する
        """
        self._listbox.delete(0, "end")
        self._current_paths = []
        self._all_paths = []

        # notes_dir が存在しない場合
        if not self._notes_dir.exists():
            self._listbox.insert("end", "（ノートがありません）")
            self._listbox.config(state="disabled")
            return

        # *.md ファイルを更新日時降順で取得
        md_files = sorted(
            self._notes_dir.glob("*.md"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

        if not md_files:
            self._listbox.insert("end", "（ノートがありません）")
            self._listbox.config(state="disabled")
            return

        self._listbox.config(state="normal")

        for path in md_files:
            mtime = datetime.fromtimestamp(path.stat().st_mtime)
            label, _ = extract_display_label(path, mtime)
            self._listbox.insert("end", label)
            self._all_paths.append(path)

        self._current_paths = list(self._all_paths)

        # フィルタ用のプルダウン選択肢を収集して更新する
        self._refresh_filter_options()

        # 1件目を自動選択
        if self._current_paths:
            self._listbox.selection_set(0)
            self._show_note(self._current_paths[0])

    def _refresh_listbox(self, paths: list[Path]) -> None:
        """指定したパスリストで Listbox を再構築する。

        検索結果の絞り込みやセマンティック検索結果の表示に使用する。

        Args:
            paths: 表示するノートのパスリスト（表示順）。
        """
        self._listbox.delete(0, "end")
        self._current_paths = []

        if not paths:
            self._listbox.insert("end", "（ヒットなし）")
            self._clear_preview()
            return

        for path in paths:
            try:
                mtime = datetime.fromtimestamp(path.stat().st_mtime)
            except Exception:
                mtime = datetime.now()
            label, _ = extract_display_label(path, mtime)
            self._listbox.insert("end", label)
            self._current_paths.append(path)

    # ------------------------------------------------------------------
    # ノート選択 → プレビュー表示
    # ------------------------------------------------------------------

    def _on_note_select(self, event: Any) -> None:
        """Listbox の選択変更イベントを処理する。"""
        selection = self._listbox.curselection()
        if not selection:
            return
        idx = selection[0]
        if idx < 0 or idx >= len(self._current_paths):
            # 「ヒットなし」等のダミー行を選択した場合
            return
        self._show_note(self._current_paths[idx])

    def _show_note(self, path: Path) -> None:
        """指定されたノートのメタ情報と本文を右ペインに表示する。

        閲覧モード（state="disabled"）で整形表示する。
        編集モード中は呼ばれないよう呼び出し側で制御すること。

        Args:
            path: 表示するノートファイルのパス。
        """
        # 編集モード中は新しいノートを選択してもプレビューを切り替えない
        # （保存 or キャンセルを先に促す）
        if self._edit_mode:
            return

        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            logger.warning(f"ノート読み込み失敗 ({path.name}): {exc}")
            self._clear_preview()
            return

        # 現在表示中のノートパスを記録する（要約再生成・保存で使う）
        self._current_note_path = path
        meta, body = parse_frontmatter(text)

        # --- メタ情報の更新 ---
        for key, var in self._meta_vars.items():
            value = meta.get(key, "")
            # リスト型（keywords など）は文字列に変換
            if isinstance(value, list):
                value = ", ".join(str(v) for v in value)
            var.set(str(value) if value else "—")

        # --- 本文プレビューの更新（整形表示）---
        # state="disabled" のまま insert はできないので一時的に "normal" にする
        self._text_widget.config(state="normal")
        self._text_widget.delete("1.0", "end")

        # Markdown タグを適用しながら挿入する（H4 の改善）
        apply_markdown_tags(self._text_widget, body, self._font_size)

        # 読み取り専用に戻す
        self._text_widget.config(state="disabled")

        # スクロールを先頭に戻す
        self._text_widget.see("1.0")

    def _clear_preview(self) -> None:
        """右ペインの表示をクリアする。"""
        self._current_note_path = None
        for var in self._meta_vars.values():
            var.set("—")
        self._text_widget.config(state="normal")
        self._text_widget.delete("1.0", "end")
        self._text_widget.config(state="disabled")

    # ------------------------------------------------------------------
    # 検索
    # ------------------------------------------------------------------

    def _on_search(self) -> None:
        """検索ボタン / Enter キーの処理。"""
        query = self._search_var.get().strip()
        mode = self._search_mode.get()

        # 空クエリ以外は検索履歴に追加する
        if query:
            self._search_history = add_to_search_history(query, self._search_history)
            save_search_history(self._search_history)
            # Combobox の選択肢を更新する
            self._search_combo["values"] = self._search_history

        if mode == "text":
            self._do_text_search(query)
        else:
            self._do_semantic_search(query)

    def _do_text_search(self, query: str) -> None:
        """テキスト検索を実行して Listbox を更新する。

        Args:
            query: 検索文字列。空文字の場合は全件表示に戻す。
        """
        if not query:
            # クエリが空 → 全件表示に戻す
            self._refresh_listbox(self._all_paths)
            if self._current_paths:
                self._listbox.selection_set(0)
                self._show_note(self._current_paths[0])
            return

        matched = search_text(query, self._all_paths)
        self._refresh_listbox(matched)

        if matched:
            self._listbox.selection_set(0)
            self._show_note(matched[0])
        else:
            self._clear_preview()

    def _do_semantic_search(self, query: str) -> None:
        """セマンティック検索を実行して Listbox を更新する。

        ChromaDB / ollama の遅延 import を伴う。
        初回呼び出し時は数秒かかるため、バックグラウンドスレッドで実行し、
        完了後に root.after でメインスレッドに結果を返す。
        検索中は1秒ごとに「セマンティック検索中... N秒」とカウントアップ表示する。

        Args:
            query: 検索文字列。
        """
        if not query:
            # クエリが空 → 全件表示に戻す
            self._refresh_listbox(self._all_paths)
            if self._current_paths:
                self._listbox.selection_set(0)
                self._show_note(self._current_paths[0])
            return

        # --- race condition 対策: このリクエストに一意な ID を振る ---
        # 古いリクエストの結果がより新しいリクエストの結果を上書きしないようにする。
        self._search_request_id += 1
        my_id = self._search_request_id

        # 検索中メッセージを表示（初期表示は "0秒"）
        self._listbox.delete(0, "end")
        self._current_paths = []
        self._listbox.insert("end", "セマンティック検索中... 0秒")

        # --- カウントアップタイマーを開始する ---
        # 検索開始時刻を記録し、1秒ごとに経過秒数を更新する
        self._search_start_time = time.monotonic()
        self._search_timer_active = True
        self._tick_search_timer(my_id)

        def _run() -> None:
            """バックグラウンドスレッドで検索を実行する。"""
            try:
                from search_lib import search_semantic
                results = search_semantic(
                    query=query,
                    top_k=10,
                    settings=self._settings,
                )
                self._semantic_available = True
            except ImportError as exc:
                # chromadb / ollama が未インストール
                # タイマーを停止してからエラーを表示する
                self._search_timer_active = False
                # 古いリクエストなら UI に反映しない
                if my_id != self._search_request_id:
                    return
                self.root.after(
                    0,
                    lambda: messagebox.showerror(
                        "セマンティック検索エラー",
                        f"必要なライブラリが見つかりません:\n{exc}\n\n"
                        "`pip install chromadb ollama` を実行してください。",
                    ),
                )
                self.root.after(0, lambda: self._refresh_listbox(self._all_paths))
                return
            except Exception as exc:
                # タイマーを停止してからエラーを表示する
                self._search_timer_active = False
                # 古いリクエストなら UI に反映しない
                if my_id != self._search_request_id:
                    return
                self.root.after(
                    0,
                    lambda: messagebox.showerror(
                        "セマンティック検索エラー",
                        f"検索中にエラーが発生しました:\n{exc}",
                    ),
                )
                self.root.after(0, lambda: self._refresh_listbox(self._all_paths))
                return

            # 結果からノートのパスリストを組み立てる
            # note_path が None のもの（DB には存在するが .md が見つからない）は除外する
            result_paths: list[Path] = []
            seen: set[Path] = set()
            for item in results:
                note_path = item.get("note_path")
                if note_path is not None and note_path not in seen:
                    result_paths.append(note_path)
                    seen.add(note_path)

            # 検索完了 → タイマーを停止する
            self._search_timer_active = False

            # メインスレッドで Listbox を更新する（request_id を渡して古い結果を除外できるようにする）
            self.root.after(0, lambda: self._on_semantic_results(result_paths, query, my_id))

        threading.Thread(target=_run, daemon=True).start()

    def _tick_search_timer(self, request_id: int) -> None:
        """セマンティック検索中の経過秒数表示を1秒ごとに更新する。

        root.after で自分自身を再帰的に予約することでカウントアップを実現する。
        _search_timer_active が False になったら（検索完了 or エラー）停止する。
        また、request_id が現在の _search_request_id と異なる（古いリクエスト）場合も停止する。

        Args:
            request_id: このカウントアップを開始したリクエストの ID。
        """
        # タイマーが無効化された or 古いリクエストなら止める
        if not self._search_timer_active or request_id != self._search_request_id:
            return

        # 経過秒数を計算してListboxの先頭行を更新する
        elapsed = int(time.monotonic() - self._search_start_time)
        # Listbox の最初の要素だけ更新する（delete + insert で書き換え）
        self._listbox.delete(0, 0)
        self._listbox.insert(0, f"セマンティック検索中... {elapsed}秒")

        # 1秒後に再度この関数を呼ぶ（UIスレッドをブロックしない after によるタイマー）
        self.root.after(1000, lambda: self._tick_search_timer(request_id))

    def _on_semantic_results(self, result_paths: list[Path], query: str, request_id: int) -> None:
        """セマンティック検索の結果をメインスレッドで Listbox に反映する。

        race condition 対策として request_id を確認し、最後に発行したリクエストの
        結果だけを UI に反映する。古いリクエストの結果は無視して返る。

        Args:
            result_paths: 検索結果のノートパスリスト（スコア降順）。
            query: 実行した検索クエリ（情報表示用）。
            request_id: このリクエストを発行した時点の _search_request_id 値。
        """
        # 最後に発行されたリクエストと異なる ID なら古い結果 → 捨てる
        if request_id != self._search_request_id:
            return

        self._refresh_listbox(result_paths)
        if result_paths:
            self._listbox.selection_set(0)
            self._show_note(result_paths[0])
        else:
            self._clear_preview()
            messagebox.showinfo(
                "セマンティック検索",
                f"「{query}」に一致するノートが見つかりませんでした。\n\n"
                "ChromaDB にノートが投入されていない場合は\n"
                "`python scripts/ingest_db.py --all` を実行してください。",
            )

    # ------------------------------------------------------------------
    # 絞り込みフィルタ
    # ------------------------------------------------------------------

    def _refresh_filter_options(self) -> None:
        """全ノートのフロントマターを読んで参加者・テーマのプルダウンを更新する。

        ノート一覧の読み込み完了後に呼ぶ（_load_notes から呼ばれる）。
        """
        participants_set: set[str] = set()
        topics_set: set[str] = set()

        for path in self._all_paths:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
                meta, _ = parse_frontmatter(text)

                # participants は文字列またはリストの場合がある
                p_val = meta.get("participants", "")
                if isinstance(p_val, list):
                    for p in p_val:
                        if p and str(p).strip():
                            participants_set.add(str(p).strip())
                elif p_val and str(p_val).strip():
                    # カンマ区切りや・区切りを個別に分割する
                    for p in re.split(r"[,、・]", str(p_val)):
                        if p.strip():
                            participants_set.add(p.strip())

                # topic は文字列
                t_val = meta.get("topic", "")
                if t_val and str(t_val).strip():
                    topics_set.add(str(t_val).strip())

            except Exception:
                pass

        # 「すべて」を先頭に置いてソートして設定する
        participant_values = ["すべて"] + sorted(participants_set)
        topic_values = ["すべて"] + sorted(topics_set)

        self._filter_participant_combo["values"] = participant_values
        self._filter_topic_combo["values"] = topic_values

    def _apply_filter(self) -> None:
        """参加者・テーマ・日付フィルタを適用して Listbox を絞り込む。

        フィルタ条件に合致するノートだけを Listbox に表示する。
        複数フィルタの AND 条件で絞り込む。
        """
        selected_participant = self._filter_participant.get()
        selected_topic = self._filter_topic.get()
        selected_date = self._filter_date.get()

        # 日付フィルタの基準日を計算する
        today = datetime.now().date()
        week_start = today - timedelta(days=today.weekday())  # 今週の月曜日

        filtered: list[Path] = []
        for path in self._all_paths:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
                meta, _ = parse_frontmatter(text)

                # --- 参加者フィルタ ---
                if selected_participant != "すべて":
                    p_val = meta.get("participants", "")
                    if isinstance(p_val, list):
                        p_str = " ".join(str(p) for p in p_val)
                    else:
                        p_str = str(p_val)
                    if selected_participant not in p_str:
                        continue  # 条件に合わないのでスキップ

                # --- テーマフィルタ ---
                if selected_topic != "すべて":
                    t_val = str(meta.get("topic", ""))
                    if selected_topic not in t_val:
                        continue  # 条件に合わないのでスキップ

                # --- 日付フィルタ ---
                if selected_date != "all":
                    # フロントマターの date または recorded_at から日付を取得する
                    date_str = str(meta.get("date", "") or meta.get("recorded_at", ""))
                    # ファイルの更新日時をフォールバックとして使う
                    if date_str:
                        # YYYY-MM-DD 形式の部分を取り出す
                        date_match = re.search(r"(\d{4}-\d{2}-\d{2})", date_str)
                        if date_match:
                            note_date = datetime.strptime(date_match.group(1), "%Y-%m-%d").date()
                        else:
                            note_date = datetime.fromtimestamp(path.stat().st_mtime).date()
                    else:
                        note_date = datetime.fromtimestamp(path.stat().st_mtime).date()

                    if selected_date == "today" and note_date != today:
                        continue  # 今日以外はスキップ
                    elif selected_date == "week" and note_date < week_start:
                        continue  # 今週以外はスキップ

                # すべての条件を通過したのでリストに追加
                filtered.append(path)

            except Exception:
                # フロントマター読み込み失敗のノートは表示する（除外しない）
                filtered.append(path)

        self._refresh_listbox(filtered)
        if filtered:
            self._listbox.selection_set(0)
            self._show_note(filtered[0])

    def _reset_filter(self) -> None:
        """絞り込みフィルタをリセットして全件表示に戻す。"""
        self._filter_participant.set("すべて")
        self._filter_topic.set("すべて")
        self._filter_date.set("all")
        self._refresh_listbox(self._all_paths)
        if self._current_paths:
            self._listbox.selection_set(0)
            self._show_note(self._current_paths[0])

    # ------------------------------------------------------------------
    # 要約の再生成（M2）
    # ------------------------------------------------------------------

    def _on_regenerate_summary(self) -> None:
        """「↻ 要約を作り直す」ボタンの処理。

        フロントマターの source キーから元 transcript のパスを特定し、
        summarize.py をサブプロセスで実行して要約を再生成する。
        実行中はボタンを無効化して進行中であることを示す。
        """
        if self._current_note_path is None:
            messagebox.showinfo("要約再生成", "ノートを選択してから実行してください。")
            return

        # --- フロントマターから source（元 transcript のファイル名）を取得 ---
        try:
            text = self._current_note_path.read_text(encoding="utf-8", errors="replace")
            meta, _ = parse_frontmatter(text)
        except Exception as exc:
            messagebox.showerror("エラー", f"ノートの読み込みに失敗しました:\n{exc}")
            return

        source = str(meta.get("source", "")).strip()
        if not source:
            messagebox.showwarning(
                "元ファイルが不明",
                "このノートには元の文字起こしファイル情報（source フィールド）がありません。\n\n"
                "手動で summarize.py を実行してください:\n"
                "  python scripts/summarize.py <文字起こしファイルのパス>",
            )
            return

        # --- transcript パスを解決する ---
        # source はファイル名（例: meeting_001_2026-05-13.txt）の場合と
        # フルパスの場合がある。transcripts_dir 配下で探す。
        source_path = Path(source)
        if source_path.is_absolute():
            # フルパスの場合はそのまま使う
            transcript_path = source_path
        else:
            # ファイル名の場合は transcripts_dir 配下に解決する
            transcript_path = self._transcripts_dir / source_path.name

        # ファイルが存在するか確認する
        if not transcript_path.exists():
            messagebox.showwarning(
                "元ファイルが見つかりません",
                f"元の文字起こしファイルが見つかりません:\n{transcript_path}\n\n"
                "ファイルが別の場所にある場合は手動で実行してください:\n"
                "  python scripts/summarize.py <文字起こしファイルのパス>",
            )
            return

        # --- セキュリティ: パスが transcripts_dir 配下にあるか確認する ---
        # subprocess に渡す前に不正なパスでないかをチェックする
        try:
            transcript_path.resolve().relative_to(self._transcripts_dir.resolve())
        except ValueError:
            # transcripts_dir の外側は許可しない
            messagebox.showerror(
                "セキュリティエラー",
                "文字起こしファイルのパスが不正です（transcripts_dir の外側を指しています）。",
            )
            return

        # --- ボタンを無効化して実行中であることを示す ---
        self._regenerate_btn.config(state="disabled", text="要約を作り直し中...")

        def _run() -> None:
            """バックグラウンドスレッドで summarize.py を実行する。"""
            try:
                summarize_script = _SCRIPTS_DIR / "summarize.py"
                # shell=False + リスト引数でシェルインジェクションを防ぐ
                result = subprocess.run(
                    [sys.executable, str(summarize_script), str(transcript_path)],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=180,  # 最大3分で打ち切る
                )
                success = result.returncode == 0
                error_msg = result.stderr if not success else ""
            except subprocess.TimeoutExpired:
                success = False
                error_msg = "要約処理がタイムアウトしました（3分超過）。"
            except Exception as exc:
                success = False
                error_msg = str(exc)

            # メインスレッドで UI を更新する
            self.root.after(0, lambda: self._on_regenerate_done(success, error_msg))

        threading.Thread(target=_run, daemon=True).start()

    def _on_regenerate_done(self, success: bool, error_msg: str) -> None:
        """要約再生成完了後にメインスレッドで呼ばれるコールバック。

        Args:
            success:   summarize.py が正常終了したかどうか。
            error_msg: エラー時のメッセージ（正常終了時は空文字列）。
        """
        # ボタンを元に戻す
        self._regenerate_btn.config(state="normal", text="↻ 要約を作り直す")

        if success:
            # 再生成成功 → ノート一覧を更新してプレビューを再読み込みする
            self._load_notes()
            messagebox.showinfo(
                "要約再生成完了",
                "要約を作り直しました。\n\n"
                "ChromaDB への再投入が必要な場合は:\n"
                "  python scripts/ingest_db.py --all\n"
                "を実行してください。",
            )
        else:
            messagebox.showerror(
                "要約再生成エラー",
                f"要約の再生成に失敗しました:\n\n{error_msg}",
            )

    # ------------------------------------------------------------------
    # ノートの簡易編集（M3）
    # ------------------------------------------------------------------

    def _on_edit_toggle(self) -> None:
        """「編集」ボタンの処理。編集モードを ON にする。"""
        if self._current_note_path is None:
            messagebox.showinfo("編集", "ノートを選択してから実行してください。")
            return

        if self._edit_mode:
            # すでに編集モード中なら何もしない
            return

        # --- 編集モード ON ---
        self._edit_mode = True

        # 生 Markdown 全文（フロントマター込み）を Text ウィジェットに表示する
        try:
            raw_text = self._current_note_path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            messagebox.showerror("エラー", f"ノートの読み込みに失敗しました:\n{exc}")
            self._edit_mode = False
            return

        # 編集可能状態にして生テキストを挿入する
        self._text_widget.config(state="normal")
        self._text_widget.delete("1.0", "end")
        self._text_widget.insert("1.0", raw_text)
        # カーソルを先頭に置く
        self._text_widget.see("1.0")

        # ボタンの状態を切り替える
        self._edit_btn.config(state="disabled")
        self._save_btn.config(state="normal")
        self._cancel_btn.config(state="normal")

    def _on_save_note(self) -> None:
        """「保存」ボタン / Ctrl+S の処理。

        アトミック書き込み（tmpに書いて os.replace）でノートを上書き保存する。
        保存後は閲覧モードに戻して整形プレビューを再表示する。
        """
        if not self._edit_mode or self._current_note_path is None:
            return

        # Text ウィジェットから編集後の全文を取得する
        # "1.0" から "end-1c" で末尾の余分な改行を除く
        new_text = self._text_widget.get("1.0", "end-1c")

        # --- アトミック書き込み ---
        # 一時ファイルに書いてから rename することで、
        # 書き込み中のクラッシュでファイルが壊れるリスクを防ぐ
        try:
            tmp_path = self._current_note_path.with_suffix(".tmp")
            tmp_path.write_text(new_text, encoding="utf-8")
            os.replace(str(tmp_path), str(self._current_note_path))
        except Exception as exc:
            messagebox.showerror("保存エラー", f"ノートの保存に失敗しました:\n{exc}")
            # 一時ファイルが残っていたら削除する
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except Exception:
                pass
            return

        # --- 閲覧モードに戻す ---
        self._edit_mode = False
        self._edit_btn.config(state="normal")
        self._save_btn.config(state="disabled")
        self._cancel_btn.config(state="disabled")

        # 整形プレビューを再表示する
        self._show_note(self._current_note_path)

        # 保存完了メッセージを表示する（ChromaDB 再投入を促す情報も添える）
        messagebox.showinfo(
            "保存完了",
            "ノートを保存しました。\n\n"
            "内容を変更した場合は ChromaDB への再投入をお勧めします:\n"
            "  python scripts/ingest_db.py --all",
        )

    def _on_cancel_edit(self) -> None:
        """「キャンセル」ボタンの処理。編集モードを終了して元の表示に戻す。"""
        if not self._edit_mode:
            return

        # --- 閲覧モードに戻す ---
        self._edit_mode = False
        self._edit_btn.config(state="normal")
        self._save_btn.config(state="disabled")
        self._cancel_btn.config(state="disabled")

        # 保存前の状態に戻すために再度プレビュー表示する
        if self._current_note_path:
            self._show_note(self._current_note_path)

    # ------------------------------------------------------------------
    # エディタで開く
    # ------------------------------------------------------------------

    def _open_in_editor(self) -> None:
        """選択中のノートを OS の既定アプリで開く（Windows 専用）。

        何も選択していない場合は何もしない。
        """
        selection = self._listbox.curselection()
        if not selection:
            return
        idx = selection[0]
        if idx < 0 or idx >= len(self._current_paths):
            return

        path = self._current_paths[idx]
        try:
            os.startfile(str(path))
        except Exception as exc:
            messagebox.showerror(
                "エラー",
                f"ファイルを開けませんでした:\n{path}\n\n{exc}",
            )


# ===========================================================================
# エントリポイント
# ===========================================================================

def main() -> int:
    """エントリポイント。"""
    try:
        app = NoteViewerApp()
    except Exception as exc:
        # 起動失敗時は messagebox で通知（pythonw 起動でも見える）
        try:
            tmp = tk.Tk()
            tmp.withdraw()
            messagebox.showerror(
                "起動エラー",
                f"ノートビューアを起動できませんでした:\n{exc}",
            )
            tmp.destroy()
        except Exception:
            print(f"[note_viewer] 起動エラー: {exc}", file=sys.stderr)
        return 1

    app.root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
