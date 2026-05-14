"""note_viewer.py
PersonalRAG の簡易ノートビューア。

Open WebUI を起動せずに data/notes/*.md を一覧・プレビュー・検索できる
軽量な tkinter アプリ。

主な機能:
    - ノート一覧表示（更新日時降順、フロントマターの title を表示）
    - ノート選択 → 右ペインにメタ情報 + 本文プレビュー
    - テキスト検索（全ファイルを開いて部分一致、大文字小文字無視）
    - セマンティック検索（ChromaDB、初回選択時に遅延 import）
    - 「エディタで開く」ボタンで既定アプリを起動
    - 「更新」ボタンでノート一覧を再読み込み

注意事項:
    - Windows 専用（「エディタで開く」が os.startfile を使用）
    - セマンティック検索は ChromaDB + Ollama が起動済みの場合のみ動作
    - ノートは通常数 KB〜数十 KB のため、ファイル読み込みは同期処理で実施

使い方:
    python scripts/note_viewer.py
    または scripts/note_viewer.cmd をダブルクリック
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from datetime import datetime
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

        # --- GUI 構築 ---
        self.root = tk.Tk()
        self.root.title("PersonalRAG ノートビューア")
        self.root.geometry("1000x700")
        self.root.resizable(True, True)
        self._build_window()

        # 起動時にノート一覧を読み込む
        self._load_notes()

    # ------------------------------------------------------------------
    # GUI 構築
    # ------------------------------------------------------------------

    def _build_window(self) -> None:
        """ウィンドウ全体のレイアウトを構築する。

        左ペイン（1/3）: 検索バー + 検索モード + ノート一覧 + 操作ボタン
        右ペイン（2/3）: メタ情報ラベル + 本文テキストウィジェット
        """
        # --- ルートの grid 設定 ---
        self.root.columnconfigure(0, weight=1)
        self.root.columnconfigure(1, weight=2)
        self.root.rowconfigure(0, weight=1)

        # --- 左ペイン ---
        left_frame = ttk.Frame(self.root, padding=8)
        left_frame.grid(row=0, column=0, sticky="nsew")
        left_frame.rowconfigure(3, weight=1)  # Listbox を伸縮させる行
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
        # --- 検索バー ---
        search_frame = ttk.Frame(frame)
        search_frame.grid(row=0, column=0, sticky="we", pady=(0, 4))
        search_frame.columnconfigure(0, weight=1)

        self._search_var = tk.StringVar()
        search_entry = ttk.Entry(search_frame, textvariable=self._search_var)
        search_entry.grid(row=0, column=0, sticky="we")
        search_entry.bind("<Return>", lambda e: self._on_search())

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

        # --- ノート一覧ラベル ---
        ttk.Label(frame, text="ノート一覧", font=("", 9, "bold")).grid(
            row=2, column=0, sticky="w", pady=(0, 2)
        )

        # --- Listbox + スクロールバー ---
        list_frame = ttk.Frame(frame)
        list_frame.grid(row=3, column=0, sticky="nsew")
        list_frame.rowconfigure(0, weight=1)
        list_frame.columnconfigure(0, weight=1)

        self._listbox = tk.Listbox(
            list_frame,
            selectmode="single",
            font=("", 9),
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
        btn_frame.grid(row=4, column=0, sticky="we", pady=(6, 0))

        ttk.Button(
            btn_frame, text="更新", command=self._load_notes, width=8
        ).pack(side="left")
        ttk.Button(
            btn_frame, text="エディタで開く", command=self._open_in_editor, width=14
        ).pack(side="left", padx=(8, 0))

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

        # --- 本文プレビューエリア ---
        text_frame = ttk.Frame(frame)
        text_frame.grid(row=1, column=0, sticky="nsew")
        text_frame.rowconfigure(0, weight=1)
        text_frame.columnconfigure(0, weight=1)

        self._text_widget = tk.Text(
            text_frame,
            wrap="word",
            font=("", 10),
            state="disabled",   # 読み取り専用（コピーは可能）
            relief="flat",
            padx=4,
            pady=4,
        )
        self._text_widget.grid(row=0, column=0, sticky="nsew")

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

        Args:
            path: 表示するノートファイルのパス。
        """
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            logger.warning(f"ノート読み込み失敗 ({path.name}): {exc}")
            self._clear_preview()
            return

        meta, body = parse_frontmatter(text)

        # --- メタ情報の更新 ---
        for key, var in self._meta_vars.items():
            value = meta.get(key, "")
            # リスト型（keywords など）は文字列に変換
            if isinstance(value, list):
                value = ", ".join(str(v) for v in value)
            var.set(str(value) if value else "—")

        # --- 本文プレビューの更新 ---
        # state="disabled" のまま insert はできないので一時的に "normal" にする
        self._text_widget.config(state="normal")
        self._text_widget.delete("1.0", "end")
        self._text_widget.insert("1.0", body)
        # 末尾の余分な改行を削除
        self._text_widget.config(state="disabled")

        # スクロールを先頭に戻す
        self._text_widget.see("1.0")

    def _clear_preview(self) -> None:
        """右ペインの表示をクリアする。"""
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

        # 検索中メッセージを表示
        self._listbox.delete(0, "end")
        self._current_paths = []
        self._listbox.insert("end", "セマンティック検索中...")

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

            # メインスレッドで Listbox を更新する（request_id を渡して古い結果を除外できるようにする）
            self.root.after(0, lambda: self._on_semantic_results(result_paths, query, my_id))

        threading.Thread(target=_run, daemon=True).start()

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
