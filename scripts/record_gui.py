"""record_gui.py
Step 1（補助）: マイク録音を「ボタン・ホットキー・タスクトレイ」で操作できる GUI。

scripts/record_mic.py の CLI 版と同じ Recorder クラス（scripts/recorder.py）を
使って録音するので、保存先や品質設定は完全に共通。

主な機能:
    - tkinter ウィンドウの大きなトグルボタンで録音 ON/OFF
    - グローバルホットキー (デフォルト Ctrl+Alt+R) でウィンドウ非アクティブでも操作可
    - タスクトレイ常駐アイコン (グレー=待機 / 赤=録音中)、メニューから操作・終了
    - マイクデバイス選択プルダウン (停止中のみ操作可)
    - 録音開始前にタイトル・参加者・テーマを入力するメモダイアログ
    - タイトルをファイル名とサイドカー meta.json に反映する
    - 10秒間連続で無音を検知したらステータス赤字表示 + 一度だけ通知ダイアログ
    - 保存先は config/settings.yaml の paths.recordings_dir (NAS の UNC パス可)
    - サービス管理カードで Ollama / Pipeline / Open WebUI を起動・停止・状態確認

スレッド構成:
    main              : tkinter mainloop（ウィジェット操作はすべてここから）
    recorder worker   : Recorder.start() 内部で起動 (sounddevice + soundfile)
    pystray           : icon.run_detached() で別スレッド
    hotkey thread     : win_hotkey.GlobalHotkey (RegisterHotKey + GetMessageW)
    service poll thread: 5 秒おきに service_manager.check_all() を呼ぶ専用スレッド

すべての操作要求は queue.Queue (command_queue) に集約し、main の _tick() で
取り出して処理する。tkinter ウィジェットは絶対に main スレッド以外から触らない。

サービスポーリングの設計:
    requests.get() を _tick() 内で同期実行すると、タイムアウト 2 秒×3 サービス = 最悪
    6 秒間 GUI が freeze する。これを避けるため、専用スレッドがバックグラウンドで
    check_all() を呼び、結果を _service_status_cache に格納する。
    _tick() はキャッシュを読むだけ（I/O なし）なので freeze しない。
"""

from __future__ import annotations

import enum
import json
import logging
import os
import queue
import shutil
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import sounddevice as sd

from config_loader import load_settings, resolve_path, update_settings_path, PROJECT_ROOT
from desktop_bridge import (
    INPUT_AUDIO,
    INPUT_TEXT,
    classify_input_file,
    copy_file_unique,
    is_pipeline_state_fresh,
)
from note_viewer import extract_display_label, parse_frontmatter, search_text
from recorder import Recorder
from remote_services import (
    ACTION_REFRESH,
    ACTION_START,
    ACTION_STOP,
    SERVICE_ALL,
    SERVICE_NAMES,
    create_service_command,
    get_control_dir,
    read_service_status,
)
from service_manager import ServiceManager, ServiceInfo, ServiceStatus

# GUI に依存しない純粋な関数は recording_meta.py に切り出してある。
# record_gui.py はそこから import して使う。
# テストは recording_meta.py を直接 import するため、ここに複製する必要はない。
from recording_meta import (
    sanitize_title,
    load_title_history,
    save_title_history,
    add_title_to_history,
    save_meta_json,
    HISTORY_MAX,
)

# ロガー（pythonw 起動時は stdout が無いため、ファイル出力を使う）
logger = logging.getLogger(__name__)


# --- 外部ライブラリ (任意機能): 未インストールでも GUI 本体は起動できるようにする ---
try:
    import pystray
    from PIL import Image, ImageDraw
except ImportError:  # pragma: no cover - 起動時に分かる
    pystray = None  # type: ignore[assignment]
    Image = None  # type: ignore[assignment]
    ImageDraw = None  # type: ignore[assignment]

# グローバルホットキーは Windows 標準 API (RegisterHotKey) を直接叩く実装を使う。
# keyboard ライブラリは「登録は成功するが反応しない」症状が出やすかったため撤去。
from win_hotkey import GlobalHotkey

# Windows 10/11 のトースト通知 (任意機能)。pystray の notify() は Windows 11 で
# ほぼ表示されないため、winotify で本物のトースト通知を出す。
try:
    from winotify import Notification as _WinotifyNotification
except ImportError:  # pragma: no cover - 未インストール時はトーストをスキップ
    _WinotifyNotification = None  # type: ignore[assignment]

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
except ImportError:  # pragma: no cover - optional desktop nicety
    DND_FILES = None  # type: ignore[assignment]
    TkinterDnD = None  # type: ignore[assignment]


class AppState(enum.Enum):
    """GUI の表示状態。録音実体の状態は Recorder.is_running() を正とし、
    こちらは UI 表示用キャッシュとして扱う。"""

    IDLE = "idle"
    RECORDING = "recording"


# command_queue に積むコマンド種別の定数
COMMAND_TOGGLE = "toggle"   # 録音開始/停止のトグル
COMMAND_SHOW = "show"       # トレイから「表示」を選んだとき
COMMAND_QUIT = "quit"       # トレイから「終了」を選んだとき


class RecordingApp:
    """録音 GUI のメインアプリ。"""

    def __init__(self) -> None:
        # --- 設定読み込み ---
        settings = load_settings()
        self._settings = settings
        rec_cfg = settings["recording"]
        paths_cfg = settings["paths"]
        pipeline_cfg = settings.get("pipeline", {})
        ui_cfg = settings.get("ui", {})
        self.recordings_dir: Path = resolve_path(settings["paths"]["recordings_dir"])
        self._pipeline_input_dir: Path = resolve_path(settings["paths"]["input_dir"])
        self._input_text_dir: Path = resolve_path(settings["paths"]["input_text_dir"])
        self._published_notes_dir: Path = resolve_path(
            paths_cfg.get("published_notes_dir", paths_cfg.get("notes_dir", "data/notes"))
        )
        self._audio_extensions = pipeline_cfg.get(
            "watch_extensions", [".wav", ".mp3", ".m4a", ".flac", ".ogg"]
        )
        self._text_extensions = pipeline_cfg.get(
            "text_extensions", [".txt", ".vtt", ".docx", ".md"]
        )
        self._pipeline_stale_seconds = int(ui_cfg.get("pipeline_stale_seconds", 30))
        self._local_service_management = bool(ui_cfg.get("local_service_management", False))
        self._remote_control_dir: Path = get_control_dir(settings)
        self.hotkey: str = rec_cfg.get("hotkey", "ctrl+alt+r")

        # --- パイプライン状態ファイルのパス（pipeline.py が書き出すファイルを読む） ---
        state_file_rel = paths_cfg.get("remote_pipeline_state_file") or pipeline_cfg.get(
            "state_file", "data/logs/pipeline_state.json"
        )
        self._pipeline_state_file: Path = resolve_path(state_file_rel)
        # 最後に状態ファイルを読んだ時刻（1 秒スロットリング用）
        self._pipeline_state_last_read: float = 0.0

        # --- 失敗ファイル管理 ---
        # failed_files.json のパス（pipeline.py が書き出す）
        self._failed_files_log: Path = resolve_path(
            pipeline_cfg.get("failed_files_log", "data/logs/failed_files.json")
        )
        # 入力フォルダ内の failed/ サブフォルダパス（隔離先）
        # 音声とテキストで別々のフォルダを管理する:
        #   audio: data/input/failed/
        #   text:  data/input_text/failed/
        # failed_files.json のエントリの source_type で振り分ける
        self._failed_dirs: dict[str, Path] = {
            "audio": resolve_path(settings["paths"]["input_dir"]) / "failed",
            "text": resolve_path(settings["paths"]["input_text_dir"]) / "failed",
        }
        # 最後に失敗件数を読んだ時刻（5 秒スロットリング用）
        self._failed_count_last_read: float = 0.0
        # 現在の隔離済みファイル件数（ボタンラベルに表示）
        self._failed_count: int = 0
        # 「隔離ファイル」ボタンのウィジェット参照（_update_failed_count_label で更新）
        self._failed_files_btn: Any = None

        # --- 録音エンジン ---
        self.recorder = Recorder(
            sample_rate=int(rec_cfg["sample_rate"]),
            channels=int(rec_cfg["channels"]),
            silence_threshold=float(rec_cfg.get("silence_threshold", 0.001)),
            silence_timeout=float(rec_cfg.get("silence_timeout", 10.0)),
            voice_loss_timeout=float(rec_cfg.get("voice_loss_timeout", 60.0)),
        )

        # --- 状態 ---
        self.command_queue: queue.Queue[str] = queue.Queue()
        self.state: AppState = AppState.IDLE
        self.selected_device: int | None = None       # None = 既定の入力デバイス
        self.silence_announced: bool = False          # 通知ダイアログの重複抑止
        self.current_output: Path | None = None       # 直近の保存先パス
        self.device_index_map: list[int | None] = []  # Combobox の表示順 → device index

        # --- 録音前メモ（フェーズ B）---
        # 直近の録音に紐付いたメタ情報（ダイアログで入力した内容）
        self._current_meta_title: str = ""
        self._current_meta_participants: str = ""
        self._current_meta_topic: str = ""
        # タイトル履歴（最大 5 件、最新が先頭）
        self._title_history: list[str] = load_title_history()

        # --- サービス管理（フェーズ C）---
        self.service_manager = ServiceManager(PROJECT_ROOT, settings)
        # ポーリングスレッドが書き込み、_tick() が読む共有キャッシュ
        self._service_status_cache: dict[str, ServiceInfo] = {}
        self._service_status_lock = threading.Lock()
        # ポーリングスレッドの停止フラグ（Event.wait で割り込み可能な待機に使う）
        self._service_poll_stop = threading.Event()
        self._service_poll_thread: threading.Thread | None = None
        if self._local_service_management:
            # daemon=True で GUI 強制終了時にスレッドも自動終了する
            self._service_poll_thread = threading.Thread(
                target=self._poll_services, daemon=True, name="service-poll"
            )
            self._service_poll_thread.start()
            self.service_manager.start_notes_auto_sync()

        # サービス管理カードのウィジェット参照（_build_window で設定する）
        # 各行: {"status_label": Label, "detail_label": Label, "button": Button}
        self._service_widgets: dict[str, dict[str, Any]] = {}

        # 「すべて起動」「すべて停止」ボタンの連打防止フラグ
        # True の間は新しい一括操作スレッドを起動しない
        # bool は GIL 保護下で atomic な読み書きができるため Lock 不要
        self._all_services_in_progress: bool = False

        # 「すべて起動」「すべて停止」ボタンのウィジェット参照（連打防止のため保持）
        self._start_all_btn: Any = None
        self._stop_all_btn: Any = None

        # --- tooltip 管理（Bug 2 修正）---
        # キー: id(widget)、値: tk.Toplevel
        # クロージャではなくインスタンス変数で管理することで、
        # 多重生成・残留リークを防ぐ（id(widget) は同一 widget に対して一意）
        self._tooltip_windows: dict[int, tk.Toplevel] = {}

        # --- 画像（pystray 利用可能時のみ生成） ---
        self._gray_image: Any = self._make_icon_image("gray") if Image else None
        self._red_image: Any = self._make_icon_image("red") if Image else None

        # --- 手元PCコンソール状態 ---
        self._drop_history: list[dict[str, str]] = []
        self._summary_paths: list[Path] = []
        self._summary_filtered_paths: list[Path] = []
        self._selected_summary_path: Path | None = None
        self._summary_last_refresh: float = 0.0
        self._nas_last_check: float = 0.0
        self._remote_service_last_read: float = 0.0
        self._remote_service_widgets: dict[str, dict[str, Any]] = {}
        self._remote_service_message_var: tk.StringVar | None = None

        # --- GUI 構築 ---
        if TkinterDnD is not None:
            self.root = TkinterDnD.Tk()
        else:
            self.root = tk.Tk()
        self.root.title("PersonalRAG")
        self._build_window()

        # --- トレイ・ホットキー（任意機能。失敗しても録音はできる）---
        self.tray_icon: Any = None
        self.hotkey_manager: GlobalHotkey | None = None
        self.hotkey_warning: str | None = None    # 起動時の登録失敗メッセージ
        self._build_tray()
        self._bind_hotkey()

        # 100ms 周期のコマンド・状態反映ループを開始
        self.root.after(100, self._tick)

        # ホットキー登録に失敗していた場合、起動直後に一度だけ警告ダイアログを出す
        if self.hotkey_warning is not None:
            self.root.after(
                300,
                lambda: messagebox.showwarning(
                    "ショートカットキーが使えません",
                    f"録音のショートカットキー（{self.hotkey.upper()}）を登録できませんでした。\n\n"
                    "【原因】他のアプリ（Zoomなど）が同じキーを使っている可能性があります。\n\n"
                    "【対処法】\n"
                    "・競合しているアプリを終了してから PersonalRAG を再起動してください。\n"
                    "・または config/settings.yaml の recording.hotkey を別のキー（例: ctrl+alt+w）に変更してください。\n\n"
                    "ボタンとシステムトレイのメニューからは引き続き録音できます。\n\n"
                    f"（詳細: {self.hotkey_warning}）",
                ),
            )

    # ------------------------------------------------------------------
    # GUI 構築
    # ------------------------------------------------------------------

    def _configure_styles(self) -> None:
        """Apple 風ミニマルに寄せた ttk スタイルを設定する。"""
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        self._color_bg = "#F7F7F5"
        self._color_surface = "#FFFFFF"
        self._color_text = "#1D1D1F"
        self._color_muted = "#6E6E73"
        self._color_line = "#E5E5EA"
        self._color_blue = "#007AFF"
        self._color_green = "#34C759"
        self._color_orange = "#FF9500"
        self._color_red = "#FF3B30"

        # settings.yaml の ui.font_size を読んでフォントサイズを決める。
        # キーが存在しなくても動くよう .get() で安全に取得し、既定値は 10 とする。
        _ui_font_size: int = int(self._settings.get("ui", {}).get("font_size", 10))
        base_font = ("Yu Gothic UI", _ui_font_size)
        title_font = ("Yu Gothic UI", _ui_font_size + 10, "bold")
        section_font = ("Yu Gothic UI", _ui_font_size + 2, "bold")
        status_font = ("Yu Gothic UI", _ui_font_size + 5, "bold")
        small_font = ("Yu Gothic UI", max(_ui_font_size - 1, 8))

        style.configure(".", font=base_font)
        style.configure("App.TFrame", background=self._color_bg)
        style.configure(
            "Surface.TFrame",
            background=self._color_surface,
            relief="solid",
            borderwidth=1,
        )
        style.configure("Flat.TFrame", background=self._color_surface)
        style.configure("Header.TLabel", background=self._color_bg, foreground=self._color_text, font=title_font)
        style.configure("Subtle.TLabel", background=self._color_bg, foreground=self._color_muted, font=small_font)
        style.configure("Surface.TLabel", background=self._color_surface, foreground=self._color_text)
        style.configure("Muted.TLabel", background=self._color_surface, foreground=self._color_muted, font=small_font)
        style.configure("Hint.TLabel", background=self._color_surface, foreground=self._color_muted, font=small_font)
        style.configure("Section.TLabel", background=self._color_surface, foreground=self._color_text, font=section_font)
        style.configure("Status.TLabel", background=self._color_surface, foreground=self._color_text, font=status_font)
        style.configure("DangerHint.TLabel", background=self._color_surface, foreground=self._color_red, font=small_font)
        style.configure("Primary.TButton", font=("Yu Gothic UI", 11, "bold"), padding=(16, 10))
        style.configure("Secondary.TButton", padding=(12, 7))
        style.configure("Ghost.TButton", padding=(10, 6))
        style.configure("ServiceCard.TFrame", background=self._color_surface, relief="solid", borderwidth=1)
        style.configure("StepPending.TLabel", background="#F2F2F7", foreground=self._color_muted, padding=(10, 6))
        style.configure("StepActive.TLabel", background="#E5F1FF", foreground=self._color_blue, padding=(10, 6))
        style.configure("StepDone.TLabel", background="#E8F8EE", foreground="#248A3D", padding=(10, 6))
        style.configure("StepError.TLabel", background="#FFE9E6", foreground=self._color_red, padding=(10, 6))
        style.configure("Horizontal.TProgressbar", troughcolor="#E5E5EA", background=self._color_blue)
        style.configure("TNotebook", background=self._color_bg, borderwidth=0)
        style.configure("TNotebook.Tab", padding=(18, 9), background="#ECECEA", foreground=self._color_muted)
        style.map("TNotebook.Tab", background=[("selected", self._color_surface)], foreground=[("selected", self._color_text)])
        style.configure("Treeview", rowheight=28, borderwidth=0)

    def _build_window(self) -> None:
        """手元PC向けコンソールのウィジェットを配置する。"""
        self._configure_styles()
        self.root.geometry("1080x760")
        self.root.minsize(920, 660)
        self.root.resizable(True, True)

        root_frame = ttk.Frame(self.root, style="App.TFrame", padding=18)
        root_frame.pack(fill="both", expand=True)
        root_frame.columnconfigure(0, weight=1)
        root_frame.rowconfigure(2, weight=1)

        header = ttk.Frame(root_frame, style="App.TFrame")
        header.grid(row=0, column=0, sticky="we", pady=(0, 14))
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="PersonalRAG", style="Header.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(
            header,
            text=f"録音、投入、要約参照、処理状況をこの画面で管理  /  ホットキー: {self.hotkey.upper()}",
            style="Subtle.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(2, 0))
        ttk.Button(
            header,
            text="更新",
            command=self._manual_refresh_console,
            style="Ghost.TButton",
        ).grid(row=0, column=1, rowspan=2, sticky="e")

        status_bar = ttk.Frame(root_frame, style="App.TFrame")
        status_bar.grid(row=1, column=0, sticky="we", pady=(0, 14))
        for col in range(3):
            status_bar.columnconfigure(col, weight=1, uniform="status")
        self._recording_status_card_var = tk.StringVar(value="待機中")
        self._nas_status_card_var = tk.StringVar(value="確認中")
        self._pipeline_status_card_var = tk.StringVar(value="確認中")
        self._build_status_card(status_bar, 0, "録音", self._recording_status_card_var)
        self._build_status_card(status_bar, 1, "NAS", self._nas_status_card_var)
        self._build_status_card(status_bar, 2, "Pipeline", self._pipeline_status_card_var)

        self._main_notebook = ttk.Notebook(root_frame)
        self._main_notebook.grid(row=2, column=0, sticky="nsew")

        recording_panel = ttk.Frame(self._main_notebook, style="Surface.TFrame", padding=18)
        drop_panel = ttk.Frame(self._main_notebook, style="Surface.TFrame", padding=18)
        pipeline_panel = ttk.Frame(self._main_notebook, style="Surface.TFrame", padding=18)
        summary_panel = ttk.Frame(self._main_notebook, style="Surface.TFrame", padding=18)
        service_panel = ttk.Frame(self._main_notebook, style="Surface.TFrame", padding=18)

        self._main_notebook.add(recording_panel, text="録音")
        self._main_notebook.add(drop_panel, text="ファイル投入")
        self._main_notebook.add(pipeline_panel, text="処理状況")
        self._main_notebook.add(summary_panel, text="要約")
        self._main_notebook.add(service_panel, text="サービス")
        self._summary_tab_index = 3

        self._build_recording_tab(recording_panel)
        self._build_drop_tab(drop_panel)
        self._build_pipeline_panel(pipeline_panel)
        self._build_summary_tab(summary_panel)
        if self._local_service_management:
            self._build_service_tab(service_panel)
        else:
            self._build_remote_service_tab(service_panel)

        # ×ボタンの挙動: 完全終了せずトレイへ収納（トレイが無ければ確認の上で完全終了）
        self.root.protocol("WM_DELETE_WINDOW", self._on_minimize_to_tray)

        # L4: キーボード操作の拡充 — Ctrl+Tab / Ctrl+Shift+Tab でタブを前後に切り替える。
        # tkinter の TNotebook は標準では Ctrl+Tab をバインドしないため、明示的に登録する。
        self.root.bind("<Control-Tab>", self._on_ctrl_tab_next)
        self.root.bind("<Control-Shift-Tab>", self._on_ctrl_tab_prev)

        self._manual_refresh_console()

    # ------------------------------------------------------------------
    # キーボードショートカット（L4）
    # ------------------------------------------------------------------

    def _on_ctrl_tab_next(self, event: Any = None) -> str:
        """Ctrl+Tab: メインタブを次に切り替える。

        TNotebook のタブ数を取得し、最後のタブなら最初に戻る（循環）。
        戻り値 "break" は tkinter のデフォルト動作（フォーカス移動）をキャンセルするために必要。
        """
        notebook = getattr(self, "_main_notebook", None)
        if notebook is None:
            return "break"
        tabs = notebook.tabs()
        if not tabs:
            return "break"
        current_index = notebook.index("current")
        # 最後のタブなら最初に戻る
        next_index = (current_index + 1) % len(tabs)
        notebook.select(next_index)
        return "break"  # デフォルトのフォーカス移動を抑止する

    def _on_ctrl_tab_prev(self, event: Any = None) -> str:
        """Ctrl+Shift+Tab: メインタブを前に切り替える。

        最初のタブなら最後に戻る（循環）。
        """
        notebook = getattr(self, "_main_notebook", None)
        if notebook is None:
            return "break"
        tabs = notebook.tabs()
        if not tabs:
            return "break"
        current_index = notebook.index("current")
        # 最初のタブなら最後に戻る
        prev_index = (current_index - 1) % len(tabs)
        notebook.select(prev_index)
        return "break"

    def _build_status_card(
        self,
        parent: ttk.Frame,
        column: int,
        title: str,
        value_var: tk.StringVar,
    ) -> None:
        """上部ステータスバーの小さなカードを作る。"""
        card = ttk.Frame(parent, style="Surface.TFrame", padding=(14, 10))
        card.grid(row=0, column=column, sticky="we", padx=(0 if column == 0 else 10, 0))
        card.columnconfigure(0, weight=1)
        ttk.Label(card, text=title, style="Muted.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(card, textvariable=value_var, style="Status.TLabel").grid(
            row=1, column=0, sticky="w", pady=(3, 0)
        )

    def _build_recording_tab(self, frame: ttk.Frame) -> None:
        """録音コックピットを配置する。"""
        frame.columnconfigure(0, weight=1)
        frame.columnconfigure(1, weight=1)
        ttk.Label(frame, text="録音", style="Section.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w"
        )

        # M5: 記号を使って色だけに依存しない状態表示にする（■ = 停止/待機）
        self.status_var = tk.StringVar(value="■ 待機中")
        self.status_label = ttk.Label(frame, textvariable=self.status_var, style="Status.TLabel")
        self.status_label.grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 12))

        self.toggle_button = ttk.Button(
            frame,
            text="録音開始",
            command=lambda: self.command_queue.put(COMMAND_TOGGLE),
            style="Primary.TButton",
        )
        self.toggle_button.grid(row=2, column=0, sticky="we", pady=(0, 16), padx=(0, 8))
        ttk.Button(
            frame,
            text="要約を見る",
            command=self._select_summary_tab,
            style="Secondary.TButton",
        ).grid(row=2, column=1, sticky="we", pady=(0, 16), padx=(8, 0))

        # マイクデバイス選択
        ttk.Label(frame, text="マイクデバイス", style="Surface.TLabel").grid(
            row=3, column=0, columnspan=2, sticky="w"
        )
        self.device_var = tk.StringVar()
        self.device_combobox = ttk.Combobox(
            frame, textvariable=self.device_var, state="readonly"
        )
        self.device_combobox.grid(row=4, column=0, columnspan=2, sticky="we", pady=(4, 14))
        self._refresh_devices()

        ttk.Label(frame, text="保存先", style="Surface.TLabel").grid(
            row=5, column=0, columnspan=2, sticky="w"
        )
        self.path_var = tk.StringVar(value=str(self.recordings_dir))
        ttk.Label(
            frame,
            textvariable=self.path_var,
            style="Muted.TLabel",
            wraplength=820,
        ).grid(row=6, column=0, columnspan=2, sticky="w", pady=(4, 10))

        button_row = ttk.Frame(frame, style="Flat.TFrame")
        button_row.grid(row=7, column=0, columnspan=2, sticky="we")
        button_row.columnconfigure(0, weight=1)
        button_row.columnconfigure(1, weight=1)
        ttk.Button(
            button_row,
            text="保存先変更",
            command=self._on_change_recordings_dir,
            style="Secondary.TButton",
        ).grid(row=0, column=0, sticky="we", padx=(0, 6))
        ttk.Button(
            button_row,
            text="保存先を開く",
            command=lambda: self._open_directory(self.recordings_dir),
            style="Secondary.TButton",
        ).grid(row=0, column=1, sticky="we", padx=(6, 0))

    def _build_drop_tab(self, frame: ttk.Frame) -> None:
        """ファイル投入ビューを作る。"""
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(3, weight=1)
        ttk.Label(frame, text="ファイル投入", style="Section.TLabel").grid(
            row=0, column=0, sticky="w"
        )

        drop_frame = tk.Frame(
            frame,
            background="#FFFFFF",
            highlightbackground="#D1D1D6",
            highlightthickness=1,
            bd=0,
        )
        drop_frame.grid(row=1, column=0, sticky="we", pady=(12, 14), ipady=32)
        drop_frame.columnconfigure(0, weight=1)
        self._drop_zone_label = tk.Label(
            drop_frame,
            text="ここにファイルをドラッグ＆ドロップ",
            background="#FFFFFF",
            foreground=self._color_text,
            font=("Yu Gothic UI", 15, "bold"),
        )
        self._drop_zone_label.grid(row=0, column=0, sticky="we")
        tk.Label(
            drop_frame,
            text="音声は input、VTT/DOCX/TXT/MD は input_text に自動振り分け",
            background="#FFFFFF",
            foreground=self._color_muted,
            font=("Yu Gothic UI", 9),
        ).grid(row=1, column=0, sticky="we", pady=(6, 0))

        if DND_FILES is not None and hasattr(drop_frame, "drop_target_register"):
            drop_frame.drop_target_register(DND_FILES)
            drop_frame.dnd_bind("<<Drop>>", self._on_drop_files)
            if hasattr(self._drop_zone_label, "drop_target_register"):
                self._drop_zone_label.drop_target_register(DND_FILES)
                self._drop_zone_label.dnd_bind("<<Drop>>", self._on_drop_files)
        else:
            self._drop_zone_label.config(text="ドラッグ＆ドロップは未有効です")

        action_row = ttk.Frame(frame, style="Flat.TFrame")
        action_row.grid(row=2, column=0, sticky="we", pady=(0, 12))
        ttk.Button(
            action_row,
            text="ファイルを選ぶ",
            command=self._choose_input_files,
            style="Primary.TButton",
        ).pack(side="left")
        ttk.Button(
            action_row,
            text="投入先を開く",
            command=self._open_input_dirs,
            style="Secondary.TButton",
        ).pack(side="left", padx=(10, 0))

        columns = ("時刻", "ファイル", "投入先", "結果")
        self._drop_tree = ttk.Treeview(frame, columns=columns, show="headings", height=10)
        for col, width in zip(columns, [90, 360, 170, 260]):
            self._drop_tree.heading(col, text=col)
            self._drop_tree.column(col, width=width, anchor="w")
        self._drop_tree.grid(row=3, column=0, sticky="nsew")

    def _build_summary_tab(self, frame: ttk.Frame) -> None:
        """公開済み要約の一覧・検索・プレビューを作る。"""
        frame.columnconfigure(0, weight=1)
        frame.columnconfigure(1, weight=2)
        frame.rowconfigure(2, weight=1)

        ttk.Label(frame, text="要約", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        self._summary_count_var = tk.StringVar(value="0 件")
        ttk.Label(frame, textvariable=self._summary_count_var, style="Muted.TLabel").grid(
            row=0, column=1, sticky="e"
        )

        search_row = ttk.Frame(frame, style="Flat.TFrame")
        search_row.grid(row=1, column=0, columnspan=2, sticky="we", pady=(12, 10))
        search_row.columnconfigure(0, weight=1)
        self._summary_search_var = tk.StringVar()
        search_entry = ttk.Entry(search_row, textvariable=self._summary_search_var)
        search_entry.grid(row=0, column=0, sticky="we")
        search_entry.bind("<Return>", lambda _event: self._filter_summaries())
        ttk.Button(
            search_row,
            text="検索",
            command=self._filter_summaries,
            style="Secondary.TButton",
        ).grid(row=0, column=1, padx=(8, 0))
        ttk.Button(
            search_row,
            text="更新",
            command=self._load_published_summaries,
            style="Secondary.TButton",
        ).grid(row=0, column=2, padx=(8, 0))

        list_frame = ttk.Frame(frame, style="Flat.TFrame")
        list_frame.grid(row=2, column=0, sticky="nsew", padx=(0, 10))
        list_frame.rowconfigure(0, weight=1)
        list_frame.columnconfigure(0, weight=1)
        self._summary_listbox = tk.Listbox(
            list_frame,
            selectmode="single",
            activestyle="none",
            borderwidth=0,
            highlightthickness=1,
            highlightbackground="#D1D1D6",
            font=("Yu Gothic UI", 10),
        )
        self._summary_listbox.grid(row=0, column=0, sticky="nsew")
        self._summary_listbox.bind("<<ListboxSelect>>", self._on_summary_select)
        summary_scroll = ttk.Scrollbar(list_frame, orient="vertical", command=self._summary_listbox.yview)
        summary_scroll.grid(row=0, column=1, sticky="ns")
        self._summary_listbox.configure(yscrollcommand=summary_scroll.set)

        preview_frame = ttk.Frame(frame, style="Flat.TFrame")
        preview_frame.grid(row=2, column=1, sticky="nsew")
        preview_frame.rowconfigure(1, weight=1)
        preview_frame.columnconfigure(0, weight=1)
        self._summary_meta_var = tk.StringVar(value="要約を選択してください")
        ttk.Label(preview_frame, textvariable=self._summary_meta_var, style="Muted.TLabel").grid(
            row=0, column=0, sticky="we", pady=(0, 8)
        )
        self._summary_text = tk.Text(
            preview_frame,
            wrap="word",
            borderwidth=0,
            highlightthickness=1,
            highlightbackground="#D1D1D6",
            background="#FFFFFF",
            foreground="#1D1D1F",
            font=("Yu Gothic UI", 10),
            padx=12,
            pady=10,
            state="disabled",
        )
        self._summary_text.grid(row=1, column=0, sticky="nsew")
        preview_actions = ttk.Frame(preview_frame, style="Flat.TFrame")
        preview_actions.grid(row=2, column=0, sticky="we", pady=(10, 0))
        ttk.Button(
            preview_actions,
            text="開く",
            command=self._open_selected_summary,
            style="Secondary.TButton",
        ).pack(side="left")
        ttk.Button(
            preview_actions,
            text="フォルダ",
            command=lambda: self._open_directory(self._published_notes_dir),
            style="Secondary.TButton",
        ).pack(side="left", padx=(8, 0))

    def _build_pipeline_panel(self, frame: ttk.Frame) -> None:
        """パイプラインの処理進捗を見える化する。"""
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(6, weight=1)
        ttk.Label(frame, text="パイプライン進捗", style="Section.TLabel").grid(
            row=0, column=0, sticky="w"
        )

        self._pipeline_current_var = tk.StringVar(value="待機中")
        self._pipeline_current_label = ttk.Label(
            frame, textvariable=self._pipeline_current_var, style="Status.TLabel"
        )
        self._pipeline_current_label.grid(row=1, column=0, sticky="w", pady=(8, 8))

        self._pipeline_progressbar = ttk.Progressbar(
            frame, mode="indeterminate", style="Horizontal.TProgressbar"
        )
        self._pipeline_progressbar.grid(row=2, column=0, sticky="we", pady=(0, 12))
        self._pipeline_progress_active = False

        steps_frame = ttk.Frame(frame, style="Surface.TFrame")
        steps_frame.grid(row=3, column=0, sticky="we")
        for column_index in range(5):
            steps_frame.columnconfigure(column_index, weight=1)
        self._pipeline_steps = [
            ("transcribe", "文字起こし"),
            ("summarize", "要約"),
            ("ingest", "ChromaDB"),
            ("sync_webui", "WebUI同期"),
            ("done", "完了"),
        ]
        self._pipeline_step_labels: dict[str, ttk.Label] = {}
        for col, (step, label_text) in enumerate(self._pipeline_steps):
            label = ttk.Label(
                steps_frame,
                text=label_text,
                anchor="center",
                style="StepPending.TLabel",
            )
            label.grid(row=0, column=col, sticky="we", padx=(0 if col == 0 else 6, 0))
            self._pipeline_step_labels[step] = label

        self._pipeline_recent_var = tk.StringVar(value="最近の処理: — 件")
        ttk.Label(frame, textvariable=self._pipeline_recent_var, style="Hint.TLabel").grid(
            row=4, column=0, sticky="w", pady=(12, 0)
        )

        self._pipeline_events_var = tk.StringVar(value="最近のイベント: —")
        ttk.Label(
            frame,
            textvariable=self._pipeline_events_var,
            style="Muted.TLabel",
            justify="left",
            wraplength=700,
        ).grid(row=5, column=0, sticky="we", pady=(8, 0))

        action_row = ttk.Frame(frame, style="Flat.TFrame")
        action_row.grid(row=6, column=0, sticky="w", pady=(12, 0))
        ttk.Button(
            action_row,
            text="処理詳細",
            command=self._show_pipeline_detail,
            style="Secondary.TButton",
        ).pack(side="left", padx=(0, 8))

        self._failed_files_btn = ttk.Button(
            action_row,
            text="隔離ファイル (0)",
            command=self._show_failed_files_dialog,
            style="Secondary.TButton",
        )
        self._failed_files_btn.pack(side="left")

        ttk.Label(
            frame,
            text="状態はリモートPCがNASへ書き出した pipeline_state.json の鮮度で判定します。",
            style="Muted.TLabel",
        ).grid(row=7, column=0, sticky="w", pady=(6, 0))

    def _manual_refresh_console(self) -> None:
        """画面上のNAS/状態/要約を手動更新する。"""
        self._update_pipeline_status()
        self._update_nas_status()
        self._load_published_summaries()

    def _select_summary_tab(self) -> None:
        """要約タブを選択する。"""
        notebook = getattr(self, "_main_notebook", None)
        if notebook is not None:
            notebook.select(self._summary_tab_index)
            self._load_published_summaries()

    def _open_directory(self, path: Path) -> None:
        """Windows Explorer でディレクトリを開く。"""
        try:
            path.mkdir(parents=True, exist_ok=True)
            os.startfile(str(path))
        except Exception as exc:
            messagebox.showerror("フォルダを開けません", f"{path}\n\n{exc}")

    def _update_nas_status(self) -> None:
        """投入先・状態・要約公開先へのアクセス可否を上部カードへ反映する。"""
        checks = [
            self.recordings_dir,
            self._input_text_dir,
            self._pipeline_state_file.parent,
            self._published_notes_dir,
        ]
        try:
            missing = [path for path in checks if not path.exists()]
            if missing:
                self._nas_status_card_var.set("未接続/未作成")
            else:
                self._nas_status_card_var.set("接続中")
        except Exception:
            self._nas_status_card_var.set("通信なし")

    def _on_drop_files(self, event: Any) -> None:
        """DnDで受け取ったファイル群を投入先へコピーする。"""
        try:
            paths = [Path(value) for value in self.root.tk.splitlist(event.data)]
        except Exception:
            paths = [Path(str(event.data))]
        self._ingest_input_files(paths)

    def _choose_input_files(self) -> None:
        """DnD不可環境向けのファイル選択投入。"""
        chosen = filedialog.askopenfilenames(title="投入するファイルを選択")
        if not chosen:
            return
        self._ingest_input_files([Path(value) for value in chosen])

    def _ingest_input_files(self, paths: list[Path]) -> None:
        """拡張子に応じて input / input_text へコピーする。"""
        for source in paths:
            if not source.exists() or not source.is_file():
                self._add_drop_history(source, "-", "ファイルが見つかりません")
                continue
            kind = classify_input_file(source, self._audio_extensions, self._text_extensions)
            if kind == INPUT_AUDIO:
                dest_dir = self._pipeline_input_dir
                label = "input"
            elif kind == INPUT_TEXT:
                dest_dir = self._input_text_dir
                label = "input_text"
            else:
                self._add_drop_history(source, "-", "未対応の拡張子")
                continue
            try:
                dest = copy_file_unique(source, dest_dir)
                self._add_drop_history(source, label, f"投入完了: {dest.name}")
            except Exception as exc:
                self._add_drop_history(source, label, f"投入失敗: {exc}")
        self._update_nas_status()

    def _add_drop_history(self, source: Path, dest_label: str, result: str) -> None:
        """投入履歴を最大50件保持してTreeviewへ反映する。"""
        self._drop_history.append(
            {
                "time": datetime.now().strftime("%H:%M:%S"),
                "file": source.name,
                "dest": dest_label,
                "result": result,
            }
        )
        self._drop_history = self._drop_history[-50:]
        tree = getattr(self, "_drop_tree", None)
        if tree is None:
            return
        for item in tree.get_children():
            tree.delete(item)
        for entry in reversed(self._drop_history):
            tree.insert("", "end", values=(entry["time"], entry["file"], entry["dest"], entry["result"]))

    def _open_input_dirs(self) -> None:
        """投入先の親フォルダを開く。"""
        target = self._pipeline_input_dir.parent
        self._open_directory(target)

    def _load_published_summaries(self) -> None:
        """公開要約フォルダから .md 一覧を読み込む。"""
        try:
            if not self._published_notes_dir.exists():
                self._summary_paths = []
            else:
                self._summary_paths = sorted(
                    self._published_notes_dir.glob("*.md"),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )
        except Exception:
            self._summary_paths = []
        self._filter_summaries()

    def _filter_summaries(self) -> None:
        """要約一覧をテキスト検索で絞り込む。"""
        query = self._summary_search_var.get().strip() if hasattr(self, "_summary_search_var") else ""
        if query:
            self._summary_filtered_paths = search_text(query, self._summary_paths)
        else:
            self._summary_filtered_paths = list(self._summary_paths)

        listbox = getattr(self, "_summary_listbox", None)
        if listbox is None:
            return
        listbox.delete(0, tk.END)
        for path in self._summary_filtered_paths:
            try:
                mtime = datetime.fromtimestamp(path.stat().st_mtime)
                label, _meta = extract_display_label(path, mtime)
            except Exception:
                label = path.name
            listbox.insert(tk.END, label)
        self._summary_count_var.set(f"{len(self._summary_filtered_paths)} 件")
        if self._summary_filtered_paths:
            listbox.selection_set(0)
            self._show_summary(self._summary_filtered_paths[0])
        else:
            self._selected_summary_path = None
            self._summary_meta_var.set("要約がありません")
            self._set_summary_text("")

    def _on_summary_select(self, _event: Any) -> None:
        """要約一覧選択時にプレビューを更新する。"""
        selection = self._summary_listbox.curselection()
        if not selection:
            return
        index = int(selection[0])
        if index >= len(self._summary_filtered_paths):
            return
        self._show_summary(self._summary_filtered_paths[index])

    def _show_summary(self, path: Path) -> None:
        """要約ファイルを右ペインに表示する。"""
        self._selected_summary_path = path
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            meta, body = parse_frontmatter(text)
            title = str(meta.get("title") or path.stem)
            participants = str(meta.get("participants") or "")
            recorded_at = str(meta.get("recorded_at") or meta.get("date") or "")
            bits = [title]
            if participants:
                bits.append(participants)
            if recorded_at:
                bits.append(recorded_at)
            self._summary_meta_var.set("  /  ".join(bits))
            self._set_summary_text(body)
        except Exception as exc:
            self._summary_meta_var.set(path.name)
            self._set_summary_text(f"読み込みに失敗しました:\n{exc}")

    def _set_summary_text(self, text: str) -> None:
        """要約プレビューのTextを安全に更新する。"""
        widget = getattr(self, "_summary_text", None)
        if widget is None:
            return
        widget.config(state="normal")
        widget.delete("1.0", tk.END)
        widget.insert("1.0", text)
        widget.config(state="disabled")

    def _open_selected_summary(self) -> None:
        """選択中の要約を既定アプリで開く。"""
        if self._selected_summary_path is None:
            return
        try:
            os.startfile(str(self._selected_summary_path))
        except Exception as exc:
            messagebox.showerror("要約を開けません", f"{self._selected_summary_path}\n\n{exc}")

    def _build_remote_service_tab(self, frame: ttk.Frame) -> None:
        """NAS 経由のリモートサービス制御ビューを作る。"""
        frame.columnconfigure(0, weight=1)
        ttk.Label(frame, text="リモートサービス", style="Section.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        self._remote_service_message_var = tk.StringVar(
            value="リモートPCで remote_service_agent.py を起動すると、状態表示とON/OFF操作が使えます。"
        )
        ttk.Label(
            frame,
            textvariable=self._remote_service_message_var,
            style="Muted.TLabel",
            wraplength=880,
        ).grid(row=1, column=0, sticky="we", pady=(8, 14))

        action_row = ttk.Frame(frame, style="Flat.TFrame")
        action_row.grid(row=2, column=0, sticky="we", pady=(0, 12))
        ttk.Button(
            action_row,
            text="すべてON",
            command=lambda: self._send_remote_service_command(ACTION_START, SERVICE_ALL),
            style="Primary.TButton",
        ).pack(side="left")
        ttk.Button(
            action_row,
            text="すべてOFF",
            command=lambda: self._send_remote_service_command(ACTION_STOP, SERVICE_ALL),
            style="Secondary.TButton",
        ).pack(side="left", padx=(10, 0))
        ttk.Button(
            action_row,
            text="更新",
            command=lambda: self._send_remote_service_command(ACTION_REFRESH, SERVICE_ALL),
            style="Secondary.TButton",
        ).pack(side="left", padx=(10, 0))

        for row, name in enumerate(SERVICE_NAMES, start=3):
            card = ttk.Frame(frame, style="ServiceCard.TFrame", padding=12)
            card.grid(row=row, column=0, sticky="we", pady=(0, 10))
            card.columnconfigure(1, weight=1)
            ttk.Label(
                card,
                text=name,
                style="Surface.TLabel",
                font=("Yu Gothic UI", 11, "bold"),
            ).grid(row=0, column=0, sticky="w")
            status_var = tk.StringVar(value="未接続")
            detail_var = tk.StringVar(value="状態ファイル待ち")
            ttk.Label(card, textvariable=status_var, style="Status.TLabel").grid(
                row=0, column=1, sticky="e"
            )
            ttk.Label(card, textvariable=detail_var, style="Muted.TLabel", wraplength=560).grid(
                row=1, column=0, columnspan=2, sticky="we", pady=(4, 10)
            )
            btn_row = ttk.Frame(card, style="Flat.TFrame")
            btn_row.grid(row=2, column=0, columnspan=2, sticky="w")
            ttk.Button(
                btn_row,
                text="ON",
                command=lambda n=name: self._send_remote_service_command(ACTION_START, n),
                style="Secondary.TButton",
            ).pack(side="left")
            ttk.Button(
                btn_row,
                text="OFF",
                command=lambda n=name: self._send_remote_service_command(ACTION_STOP, n),
                style="Secondary.TButton",
            ).pack(side="left", padx=(8, 0))
            self._remote_service_widgets[name] = {
                "status_var": status_var,
                "detail_var": detail_var,
            }

    def _send_remote_service_command(self, action: str, service: str) -> None:
        """NASへリモートサービス操作コマンドを書き込む。"""
        try:
            command_path = create_service_command(
                self._remote_control_dir,
                action=action,
                service=service,
            )
            label = {"start": "ON", "stop": "OFF", "refresh": "更新"}.get(action, action)
            if self._remote_service_message_var is not None:
                self._remote_service_message_var.set(
                    f"{service} の {label} 要求を送信しました: {command_path.name}"
                )
        except Exception as exc:
            # 初心者向け: NAS接続問題の対処法を案内する
            messagebox.showerror(
                "リモート操作コマンドを送れませんでした",
                f"NASの制御フォルダにコマンドを書き込めませんでした。\n\n"
                "【確認してほしいこと】\n"
                "・NAS（ネットワークドライブ）への接続が切れていないか確認してください。\n"
                "・NASの制御フォルダへの書き込み権限があるか確認してください。\n\n"
                f"（詳細: {exc}）",
            )
        self._update_remote_services_status()

    def _update_remote_services_status(self) -> None:
        """remote_service_agent.py が公開したサービス状態を表示する。"""
        if self._local_service_management:
            return
        try:
            payload = read_service_status(self._remote_control_dir)
            if payload is None:
                self._set_remote_services_disconnected("状態ファイルなし")
                return
            if not is_pipeline_state_fresh(
                payload,
                stale_seconds=self._pipeline_stale_seconds,
            ):
                self._set_remote_services_disconnected("remote_service_agent 停止/通信なし")
                return

            services = {
                str(service.get("name")): service
                for service in payload.get("services", [])
                if isinstance(service, dict)
            }
            for name in SERVICE_NAMES:
                service = services.get(name, {})
                status = str(service.get("status", "unknown"))
                detail = str(service.get("detail", ""))
                widgets = self._remote_service_widgets.get(name)
                if widgets is None:
                    continue
                widgets["status_var"].set(self._format_remote_service_status(status))
                widgets["detail_var"].set(detail or "詳細なし")

            last_command = payload.get("last_command") or {}
            if self._remote_service_message_var is not None and last_command:
                ok = "成功" if last_command.get("ok") else "失敗"
                self._remote_service_message_var.set(
                    f"最終操作: {last_command.get('service')} {last_command.get('action')} / {ok} / {last_command.get('message', '')}"
                )
        except Exception as exc:
            self._set_remote_services_disconnected(f"読み込み失敗: {exc}")

    def _format_remote_service_status(self, status: str) -> str:
        """サービス状態をUI向け短文に変換する。"""
        if status == "running":
            return "ON"
        if status == "stopped":
            return "OFF"
        return "確認中"

    def _set_remote_services_disconnected(self, message: str) -> None:
        """サービス状態ファイルが読めないときの表示。"""
        for widgets in self._remote_service_widgets.values():
            widgets["status_var"].set("未接続")
            widgets["detail_var"].set(message)
        if self._remote_service_message_var is not None:
            self._remote_service_message_var.set(message)

    def _build_service_tab(self, frame: ttk.Frame) -> None:
        """サービス管理カードを配置する。

        外部起動のサービスも状態が読み取りやすいよう、1 サービス 1 カードにする。
        """
        SERVICE_NAMES = ["Ollama", "Pipeline", "Open WebUI"]
        frame.columnconfigure(0, weight=1)
        ttk.Label(frame, text="サービス", style="Section.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 8)
        )

        for row_idx, name in enumerate(SERVICE_NAMES, start=1):
            card = ttk.Frame(frame, style="ServiceCard.TFrame", padding=10)
            card.grid(row=row_idx, column=0, sticky="we", pady=(0, 8))
            card.columnconfigure(1, weight=1)

            ttk.Label(card, text=name, style="Surface.TLabel", font=("Yu Gothic UI", 10, "bold")).grid(
                row=0, column=0, sticky="w"
            )
            status_label = ttk.Label(card, text="○ 停止中", style="Hint.TLabel")
            status_label.grid(row=0, column=1, sticky="e")

            detail_label = ttk.Label(
                card,
                text="確認中...",
                style="Hint.TLabel",
                wraplength=220,
            )
            detail_label.grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 8))

            btn = ttk.Button(
                card,
                text="起動",
                style="Secondary.TButton",
                command=lambda n=name: self._on_service_button(n),
            )
            btn.grid(row=2, column=0, columnspan=2, sticky="we")

            self._service_widgets[name] = {
                "status_label": status_label,
                "detail_label": detail_label,
                "button": btn,
            }

        bulk_frame = ttk.Frame(frame, style="Surface.TFrame")
        bulk_frame.grid(row=len(SERVICE_NAMES) + 1, column=0, sticky="we", pady=(2, 0))
        bulk_frame.columnconfigure(0, weight=1)
        bulk_frame.columnconfigure(1, weight=1)

        self._start_all_btn = ttk.Button(
            bulk_frame,
            text="すべて起動",
            command=self._on_start_all_services,
            style="Secondary.TButton",
        )
        self._start_all_btn.grid(row=0, column=0, sticky="we", padx=(0, 6))

        self._stop_all_btn = ttk.Button(
            bulk_frame,
            text="すべて停止",
            command=self._on_stop_all_services,
            style="Secondary.TButton",
        )
        self._stop_all_btn.grid(row=0, column=1, sticky="we", padx=(6, 0))

        ttk.Label(
            frame,
            text=(
                "注意: 文字起こし中に Open WebUI でチャットすると、Ollama/Gemma と "
                "Whisper が VRAM を奪い合う恐れがあります。チャット前に Pipeline を停止してください。"
            ),
            style="DangerHint.TLabel",
            justify="left",
            wraplength=330,
        ).grid(
            row=len(SERVICE_NAMES) + 2, column=0, sticky="w", pady=(12, 0)
        )

    def _refresh_devices(self) -> None:
        """入力デバイス一覧を Combobox に流し込む。"""
        try:
            devices = sd.query_devices()
        except Exception as exc:
            # 初心者向け: 何が起きたか＋どう直すかを説明する
            messagebox.showerror(
                "マイクが見つかりません",
                f"マイクデバイスの一覧を取得できませんでした。\n\n"
                "【確認してほしいこと】\n"
                "・Windowsの「スタート」→「設定」→「サウンド」でマイクが有効になっているか確認してください。\n"
                "・マイクのUSBケーブルや接続が外れていないか確認してください。\n\n"
                f"（詳細: {exc}）",
            )
            return

        labels: list[str] = ["（既定の入力デバイス）"]
        self.device_index_map = [None]

        for idx, dev in enumerate(devices):
            if dev.get("max_input_channels", 0) <= 0:
                continue
            name = dev.get("name", f"Device {idx}")
            hostapi_index = dev.get("hostapi", -1)
            try:
                hostapi_name = sd.query_hostapis(hostapi_index)["name"]
            except Exception:
                hostapi_name = f"hostapi={hostapi_index}"
            labels.append(f"{idx}: {name} ({hostapi_name})")
            self.device_index_map.append(idx)

        self.device_combobox["values"] = labels
        self.device_combobox.current(0)
        self.selected_device = None

    # ------------------------------------------------------------------
    # トレイ・ホットキー
    # ------------------------------------------------------------------

    def _make_icon_image(self, color: str) -> Any:
        """64x64 の円アイコン画像を Pillow で生成する。color は "gray" または "red"。"""
        size = 64
        image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        fill = (200, 60, 60, 255) if color == "red" else (140, 140, 140, 255)
        # 円を描く（少し内側に余白）
        draw.ellipse((6, 6, size - 6, size - 6), fill=fill)
        return image

    def _build_tray(self) -> None:
        """システムトレイアイコンを別スレッドで起動。pystray が無ければスキップ。"""
        if pystray is None or self._gray_image is None:
            # pystray/Pillow が無い場合は GUI のみで動作させる
            return

        menu = pystray.Menu(
            pystray.MenuItem(
                "表示",
                lambda icon, item: self.command_queue.put(COMMAND_SHOW),
                default=True,  # アイコンダブルクリックで「表示」
            ),
            pystray.MenuItem(
                "録音/停止",
                lambda icon, item: self.command_queue.put(COMMAND_TOGGLE),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "終了",
                lambda icon, item: self.command_queue.put(COMMAND_QUIT),
            ),
        )

        self.tray_icon = pystray.Icon(
            "personalrag_recorder",
            self._gray_image,
            title="PersonalRAG 録音（待機中）",
            menu=menu,
        )
        # 別スレッドで起動（tkinter mainloop と共存させるため detached）
        self.tray_icon.run_detached()

    def _bind_hotkey(self) -> None:
        """グローバルホットキー (Windows API: RegisterHotKey) を登録する。

        登録失敗時は GUI ステータスに警告を残すだけで、アプリ自体は続行する。
        ボタン操作・トレイ操作は引き続き使える。
        """
        try:
            self.hotkey_manager = GlobalHotkey(
                self.hotkey,
                lambda: self.command_queue.put(COMMAND_TOGGLE),
            )
            self.hotkey_manager.start()
        except Exception as exc:
            # キー指定文字列のパースエラーなど
            self.hotkey_manager = None
            self.hotkey_warning = f"ホットキー設定エラー: {exc}"
            return

        if not self.hotkey_manager.is_active():
            # RegisterHotKey 自体は呼べたが、他アプリと衝突したケース等
            err = self.hotkey_manager.last_error_code()
            self.hotkey_warning = (
                f"ホットキー ({self.hotkey}) を登録できませんでした"
                f"（他アプリと衝突している可能性。Windows エラーコード: {err}）。"
                "ボタンとトレイメニューからは引き続き操作できます。"
            )

    # ------------------------------------------------------------------
    # サービス状態ポーリング（バックグラウンドスレッド）
    # ------------------------------------------------------------------

    def _poll_services(self) -> None:
        """サービス状態を 5 秒おきにポーリングするバックグラウンドスレッドの本体。

        daemon=True で起動しているため GUI 強制終了時に自動で停止する。
        通常終了時は _service_poll_stop.set() で停止を促す。
        """
        while not self._service_poll_stop.is_set():
            try:
                infos = self.service_manager.check_all()
                # ロックを取って一括更新
                with self._service_status_lock:
                    for info in infos:
                        self._service_status_cache[info.name] = info
            except Exception as exc:
                logger.warning(f"サービス状態取得失敗: {exc}")
            # 5 秒間待つ（Event.wait を使うと _service_poll_stop.set() で即抜けられる）
            self._service_poll_stop.wait(timeout=5.0)

    # ------------------------------------------------------------------
    # メインループ（コマンド処理と状態反映）
    # ------------------------------------------------------------------

    def _tick(self) -> None:
        """100ms 周期で呼ばれる。queue を drain して状態を反映する。"""
        # 1) command_queue を drain
        while not self.command_queue.empty():
            try:
                cmd = self.command_queue.get_nowait()
            except queue.Empty:
                break
            if cmd == COMMAND_TOGGLE:
                self._toggle()
            elif cmd == COMMAND_SHOW:
                self._restore_window()
            elif cmd == COMMAND_QUIT:
                self._on_quit_request()

        # 2) Recorder のエラーチェック（録音中のみ）
        if self.state == AppState.RECORDING:
            err = self.recorder.last_error()
            if err is not None:
                self._force_idle()
                # 初心者向け: 録音中断の理由と対処法を案内する
                messagebox.showerror(
                    "録音が中断されました",
                    f"録音中に問題が発生し、自動的に停止しました。\n\n"
                    "【確認してほしいこと】\n"
                    "・マイクの接続が外れていないか確認してください。\n"
                    "・保存先フォルダの空き容量が少なくなっていないか確認してください。\n"
                    "・問題が続く場合は、一度アプリを再起動してみてください。\n\n"
                    f"（詳細: {err}）",
                )

        # 3) 録音状態の表示更新
        if self.state == AppState.RECORDING:
            elapsed = int(self.recorder.elapsed())
            elapsed_str = time.strftime("%H:%M:%S", time.gmtime(elapsed))

            if self.recorder.is_silent():
                # 無音検知中は赤字 + トレイ tooltip 変更 + 一度だけ通知
                self.status_var.set(f"⚠ 音声が入っていません  {elapsed_str}")
                self.status_label.config(foreground="red")
                if self.tray_icon is not None:
                    self.tray_icon.title = f"⚠ 録音中（無音） {elapsed_str}"
                if not self.silence_announced:
                    self.silence_announced = True
                    self._show_silence_dialog()
            else:
                # 色覚特性への配慮（M5）: 色変化だけでなく「● 録音中」のように記号でも状態を示す
                self.status_var.set(f"● 録音中  {elapsed_str}")
                self.status_label.config(foreground="black")
                if self.tray_icon is not None:
                    self.tray_icon.title = f"PersonalRAG 録音中 {elapsed_str}"
        self._recording_status_card_var.set(
            "録音中" if self.state == AppState.RECORDING else "待機中"
        )

        # 4) パイプライン状態ファイルを読んで GUI を更新（最終読み込みから 1 秒以上経った時のみ）
        now = time.monotonic()
        if now - self._pipeline_state_last_read >= 1.0:
            self._pipeline_state_last_read = now
            self._update_pipeline_status()
        if now - self._nas_last_check >= 5.0:
            self._nas_last_check = now
            self._update_nas_status()
        if now - self._summary_last_refresh >= 10.0:
            self._summary_last_refresh = now
            self._load_published_summaries()
        if now - self._remote_service_last_read >= 3.0:
            self._remote_service_last_read = now
            self._update_remote_services_status()

        # 4-b) 隔離ファイルの件数を 5 秒おきに更新してボタンラベルに反映する
        if now - self._failed_count_last_read >= 5.0:
            self._failed_count_last_read = now
            self._update_failed_count_label()

        # 5) サービス管理カードの表示更新（ロック取得 → キャッシュ参照 → 即解放）
        #    I/O なし・ロック保持時間は数マイクロ秒なので freeze の心配なし
        if self._local_service_management:
            with self._service_status_lock:
                cache_snapshot = dict(self._service_status_cache)
            if cache_snapshot:
                self._update_service_tab(cache_snapshot)

        # 6) 次回呼び出しを予約
        self.root.after(100, self._tick)

    # ------------------------------------------------------------------
    # 状態遷移
    # ------------------------------------------------------------------

    def _toggle(self) -> None:
        """録音開始 ↔ 停止のトグル。"""
        if self.state == AppState.IDLE:
            self._start_recording()
        else:
            self._stop_recording()

    # ------------------------------------------------------------------
    # 録音前メモダイアログ（フェーズ B）
    # ------------------------------------------------------------------

    def _show_pre_recording_dialog(self) -> dict[str, str] | None:
        """録音開始前のメモ入力ダイアログを表示する。

        ユーザーは「録音開始」「メモなしで開始」「キャンセル」の 3 択で操作する。
        ダイアログは grab_set() でモーダルになる（他のウィンドウを操作不可にする）。

        Returns:
            「録音開始」または「メモなしで開始」が押されたら dict を返す。
            キャンセルまたは × ボタンで閉じたら None を返す。
            dict のキー: "title", "participants", "topic"
        """
        # 現在の履歴を読み込む
        history = self._title_history

        # Toplevel でモーダルダイアログを作成
        dialog = tk.Toplevel(self.root)
        dialog.title("録音情報の入力（任意）")
        dialog.resizable(False, False)
        dialog.transient(self.root)  # 親ウィンドウに関連付ける

        # 結果を格納する変数（None = キャンセル）
        result: dict[str, str] | None = None

        # --- ウィジェット構築 ---
        frame = ttk.Frame(dialog, padding=16)
        frame.pack(fill="both", expand=True)

        # タイトル（Combobox: 過去履歴がプルダウンに、自由入力も可）
        ttk.Label(frame, text="タイトル:").grid(row=0, column=0, sticky="w", pady=(0, 4))
        title_var = tk.StringVar()
        title_combo = ttk.Combobox(
            frame,
            textvariable=title_var,
            values=history,
            width=40,
        )
        title_combo.grid(row=0, column=1, sticky="we", pady=(0, 4))

        # 参加者
        ttk.Label(frame, text="参加者:").grid(row=1, column=0, sticky="w", pady=(0, 4))
        participants_var = tk.StringVar()
        ttk.Entry(frame, textvariable=participants_var, width=42).grid(
            row=1, column=1, sticky="we", pady=(0, 4)
        )

        # テーマ
        ttk.Label(frame, text="テーマ:").grid(row=2, column=0, sticky="w", pady=(0, 12))
        topic_var = tk.StringVar()
        ttk.Entry(frame, textvariable=topic_var, width=42).grid(
            row=2, column=1, sticky="we", pady=(0, 12)
        )

        frame.columnconfigure(1, weight=1)

        # --- ボタン ---
        button_frame = ttk.Frame(dialog, padding=(16, 0, 16, 16))
        button_frame.pack(fill="x")

        def _on_start_with_meta() -> None:
            """「録音開始」ボタン: メタ情報を保存して録音を開始する。"""
            nonlocal result
            title = title_var.get().strip()
            result = {
                "title": title,
                "participants": participants_var.get().strip(),
                "topic": topic_var.get().strip(),
            }
            # 空でないタイトルを履歴に追加・保存
            if title:
                self._title_history = add_title_to_history(title, self._title_history)
                save_title_history(self._title_history)
            dialog.destroy()

        def _on_start_without_meta() -> None:
            """「メモなしで開始」ボタン: メタ情報なしで即録音する。"""
            nonlocal result
            result = {"title": "", "participants": "", "topic": ""}
            dialog.destroy()

        def _on_cancel() -> None:
            """キャンセルまたは × ボタン: 録音せずに閉じる。"""
            nonlocal result
            result = None
            dialog.destroy()

        ttk.Button(
            button_frame, text="録音開始", command=_on_start_with_meta, width=14
        ).pack(side="left", padx=(0, 8))
        ttk.Button(
            button_frame, text="メモなしで開始", command=_on_start_without_meta, width=14
        ).pack(side="left", padx=(0, 8))
        ttk.Button(
            button_frame, text="キャンセル", command=_on_cancel, width=10
        ).pack(side="right")

        # × ボタンはキャンセルと同じ動作
        dialog.protocol("WM_DELETE_WINDOW", _on_cancel)

        # タイトル入力欄にフォーカスを当てる
        title_combo.focus_set()

        # モーダル化: 他のウィンドウを操作不可にする
        dialog.grab_set()

        # ダイアログが閉じるまで待つ
        self.root.wait_window(dialog)

        return result

    def _start_recording(self) -> None:
        """録音開始処理。まずメモダイアログを表示し、入力内容を取得してから録音を開始する。"""
        # 選択中のデバイスを取得
        idx = self.device_combobox.current()
        if idx < 0 or idx >= len(self.device_index_map):
            idx = 0
        self.selected_device = self.device_index_map[idx]

        # --- 録音前メモダイアログを表示 ---
        # ダイアログが「キャンセル」または「×」で閉じられた場合は録音しない
        meta = self._show_pre_recording_dialog()
        if meta is None:
            # キャンセル: 何もしない
            return

        # ダイアログで入力されたメタ情報を保持しておく（後で meta.json に保存する）
        self._current_meta_title = meta["title"]
        self._current_meta_participants = meta["participants"]
        self._current_meta_topic = meta["topic"]

        # --- ファイル名の組み立て ---
        # タイトルがある場合: rec_2026-05-15_143022_サニタイズ済みタイトル.wav
        # タイトルが空の場合: rec_2026-05-15_143022.wav
        timestamp_str = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        sanitized = sanitize_title(self._current_meta_title)
        if sanitized:
            filename = f"rec_{timestamp_str}_{sanitized}.wav"
        else:
            filename = f"rec_{timestamp_str}.wav"

        output_path = self.recordings_dir / filename
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            # NAS 切断・権限不足など
            # 初心者向け: NAS切断・権限不足など、原因ごとの対処法を案内する
            messagebox.showerror(
                "録音ファイルの保存先を準備できません",
                f"録音ファイルを保存するフォルダを作成・確認できませんでした。\n\n"
                "【確認してほしいこと】\n"
                "・NAS（ネットワークドライブ）を使っている場合、接続が切れていないか確認してください。\n"
                "・保存先フォルダへの書き込み権限があるか確認してください。\n"
                "・config/settings.yaml の paths.recordings_dir の設定が正しいか確認してください。\n\n"
                f"保存先: {output_path.parent}\n（詳細: {exc}）",
            )
            return

        # 録音開始
        try:
            self.recorder.start(output_path, device=self.selected_device)
        except Exception as exc:
            # 初心者向け: 録音開始失敗の対処法を案内する
            messagebox.showerror(
                "録音を開始できませんでした",
                f"マイクへのアクセスに失敗しました。\n\n"
                "【確認してほしいこと】\n"
                "・マイクが正しく接続されているか確認してください。\n"
                "・Windowsの「プライバシー設定」でこのアプリにマイクへのアクセス許可が与えられているか確認してください。\n"
                "・別のアプリがマイクを占有していないか確認してください（Zoomなど）。\n\n"
                f"（詳細: {exc}）",
            )
            return

        self.current_output = output_path
        self.state = AppState.RECORDING
        self.silence_announced = False

        # UI 更新（M5: 色覚特性への配慮として記号も添える）
        self.toggle_button.config(text="録音停止")
        self.device_combobox.config(state="disabled")
        self.status_var.set("● 録音中  00:00:00")
        self.status_label.config(foreground="black")
        self.path_var.set(str(output_path))
        if self.tray_icon is not None and self._red_image is not None:
            self.tray_icon.icon = self._red_image
            self.tray_icon.title = "PersonalRAG 録音中"

    def _stop_recording(self) -> None:
        """録音停止処理。meta.json 保存と無音削除ロジックを含む。"""
        # 停止前に「この録音で一度でも音声を検知したか」を確認しておく。
        # stop() 後は次の start() で内部状態がリセットされるため、停止前に取る。
        voice_detected = self.recorder.was_voice_detected()

        try:
            saved = self.recorder.stop()
        except Exception as exc:
            # 初心者向け: 停止時のエラーも分かりやすく案内する
            messagebox.showerror(
                "録音の停止に問題が発生しました",
                f"録音を停止する際にエラーが発生しました。\n"
                "録音ファイルは一部保存されている場合があります。\n\n"
                "問題が続く場合はアプリを再起動してください。\n\n"
                f"（詳細: {exc}）",
            )
            saved = self.current_output

        self.state = AppState.IDLE
        self.toggle_button.config(text="録音開始")
        self.device_combobox.config(state="readonly")
        self.status_label.config(foreground="black")

        tray_title = "PersonalRAG 録音（待機中）"

        if saved is None:
            # そもそも保存先パスが取れていない（録音開始前の異常停止など）
            # M5: 記号で状態を示す（■ = 停止）
            self.status_var.set("■ 待機中")
        elif not voice_detected:
            # 一度も音声を検知していないので WAV を削除する。
            # pipeline.py が空っぽのファイルに無駄な文字起こしを走らせるのを防ぐ。
            try:
                saved.unlink(missing_ok=True)
            except Exception:
                # 削除失敗（ファイルロック中など）は致命的ではないので握りつぶす
                pass
            # WAV を削除したら隣の meta.json も削除する（ゴミを残さない）
            meta_path = saved.parent / (saved.stem + ".meta.json")
            try:
                meta_path.unlink(missing_ok=True)
            except Exception:
                pass
            # M5: 「⚠」記号で無音を示す（色だけに頼らない）
            self.status_var.set(f"⚠ 無音のため削除しました: {saved.name}")
            self.path_var.set(str(self.recordings_dir))
            tray_title = "PersonalRAG 録音（無音破棄）"
        else:
            # 音声を検知した有効な録音: meta.json を保存してから完了表示
            save_meta_json(
                saved,
                title=self._current_meta_title,
                participants=self._current_meta_participants,
                topic=self._current_meta_topic,
            )
            try:
                saved_is_watched = saved.parent.resolve() == self._pipeline_input_dir.resolve()
            except OSError:
                saved_is_watched = False

            if saved_is_watched:
                # M5: 「✓」記号で保存成功を示す
                self.status_var.set(f"✓ 保存しました: {saved.name}")
            else:
                self.status_var.set(
                    f"✓ 保存しました: {saved.name}（Pipeline 監視対象外）"
                )
            self.path_var.set(str(saved))

        # メタ情報をリセット（次の録音セッションに引き継がない）
        self._current_meta_title = ""
        self._current_meta_participants = ""
        self._current_meta_topic = ""

        if self.tray_icon is not None and self._gray_image is not None:
            self.tray_icon.icon = self._gray_image
            self.tray_icon.title = tray_title

    def _force_idle(self) -> None:
        """エラーで録音が止まった際に UI を待機状態へ強制復帰させる。"""
        try:
            self.recorder.stop()
        except Exception:
            pass
        self.state = AppState.IDLE
        self.toggle_button.config(text="録音開始")
        self.device_combobox.config(state="readonly")
        self.status_var.set("待機中")
        self.status_label.config(foreground="black")
        if self.tray_icon is not None and self._gray_image is not None:
            self.tray_icon.icon = self._gray_image
            self.tray_icon.title = "PersonalRAG 録音（待機中）"

    # ------------------------------------------------------------------
    # パイプライン状態表示
    # ------------------------------------------------------------------

    def _update_pipeline_status(self) -> None:
        """NAS上の pipeline_state.json を読んで処理状況 UI を更新する。"""
        try:
            if not self._pipeline_state_file.exists():
                self._pipeline_status_card_var.set("通信なし")
                self._pipeline_current_var.set("状態ファイルなし")
                self._pipeline_current_label.config(foreground=self._color_red)
                self._pipeline_recent_var.set("最近の処理: —")
                self._pipeline_events_var.set("最近のイベント: —")
                self._set_pipeline_progress(False)
                self._set_pipeline_steps(active_step=None, error=True)
                return

            text = self._pipeline_state_file.read_text(encoding="utf-8")
            data = json.loads(text)
            recent = data.get("recent", [])

            if not is_pipeline_state_fresh(
                data,
                stale_seconds=self._pipeline_stale_seconds,
            ):
                self._pipeline_status_card_var.set("通信なし")
                updated_at = str(data.get("updated_at", ""))[:19].replace("T", " ")
                self._pipeline_current_var.set(f"更新停止中  /  最終更新 {updated_at or '不明'}")
                self._pipeline_current_label.config(foreground=self._color_orange)
                self._set_pipeline_progress(False)
                self._set_pipeline_steps(active_step=None, error=True)
                if recent:
                    self._update_pipeline_recent_count(recent)
                else:
                    self._pipeline_recent_var.set("最近の処理: —")
                self._update_pipeline_events(recent)
                return

            self._pipeline_status_card_var.set("稼働中")
            self._pipeline_current_label.config(foreground=self._color_text)

            # 現在処理中のファイルを表示
            current = data.get("current")
            if current:
                step_label = {
                    "transcribe": "文字起こし中",
                    "summarize": "要約中",
                    "ingest": "DB 投入中",
                    "sync_webui": "Open WebUI 同期中",
                }.get(current.get("step", ""), current.get("step", "処理中"))
                elapsed = self._format_started_elapsed(current.get("started_at", ""))
                elapsed_text = f"  /  経過 {elapsed}" if elapsed else ""
                self._pipeline_current_var.set(
                    f"{current.get('file', '')}  ({step_label}){elapsed_text}"
                )
                self._set_pipeline_progress(True)
                self._set_pipeline_steps(active_step=current.get("step"))
            else:
                queue_len = len(data.get("queue", []))
                self._pipeline_current_var.set(f"待機中  /  待ち {queue_len} 件")
                self._set_pipeline_progress(False)
                latest_result = recent[-1].get("result") if recent else None
                if latest_result == "success":
                    self._set_pipeline_steps(active_step="done")
                elif latest_result == "failed":
                    self._set_pipeline_steps(active_step=None, error=True)
                else:
                    self._set_pipeline_steps(active_step=None)

            # 直近 24 時間の成功/失敗カウント
            self._update_pipeline_recent_count(recent)
            self._update_pipeline_events(recent)

        except Exception:
            # ファイル読み込み失敗・JSON 壊れ等は全て無視してフォールバック
            self._pipeline_status_card_var.set("読み込み失敗")
            self._pipeline_current_var.set("状態ファイル読み込み失敗")
            self._pipeline_current_label.config(foreground=self._color_red)
            self._pipeline_recent_var.set("最近の処理: — （読み込み失敗）")
            self._pipeline_events_var.set("最近のイベント: 状態ファイルを読み込めませんでした")
            self._set_pipeline_progress(False)
            self._set_pipeline_steps(active_step=None, error=True)

    def _set_pipeline_progress(self, active: bool) -> None:
        """処理中だけプログレスバーを動かす。"""
        progressbar = getattr(self, "_pipeline_progressbar", None)
        if progressbar is None:
            return

        if active and not getattr(self, "_pipeline_progress_active", False):
            progressbar.start(12)
            self._pipeline_progress_active = True
        elif not active and getattr(self, "_pipeline_progress_active", False):
            progressbar.stop()
            self._pipeline_progress_active = False
            progressbar["value"] = 0

    def _set_pipeline_steps(self, active_step: str | None, error: bool = False) -> None:
        """現在のパイプラインステップをバッジ表示へ反映する。"""
        labels = getattr(self, "_pipeline_step_labels", {})
        if not labels:
            return

        order = [step for step, _label in self._pipeline_steps]
        active_index = order.index(active_step) if active_step in order else -1

        for index, step in enumerate(order):
            label = labels.get(step)
            if label is None:
                continue

            if error:
                style = "StepError.TLabel" if step == "transcribe" else "StepPending.TLabel"
            elif active_index == -1:
                style = "StepPending.TLabel"
            elif index < active_index:
                style = "StepDone.TLabel"
            elif index == active_index:
                style = "StepDone.TLabel" if step == "done" else "StepActive.TLabel"
            else:
                style = "StepPending.TLabel"
            label.config(style=style)

    def _format_started_elapsed(self, started_at_str: str) -> str:
        """started_at から経過時間の短い表示を作る。"""
        if not started_at_str:
            return ""
        try:
            started_at = datetime.fromisoformat(started_at_str)
            if started_at.tzinfo is None:
                started_at = started_at.replace(tzinfo=timezone.utc)
            elapsed = int((datetime.now(timezone.utc) - started_at).total_seconds())
            if elapsed < 0:
                elapsed = 0
            return time.strftime("%H:%M:%S", time.gmtime(elapsed))
        except Exception:
            return ""

    def _update_pipeline_events(self, recent: list[dict[str, Any]]) -> None:
        """直近の処理結果を短いイベントログとして表示する。"""
        if not recent:
            self._pipeline_events_var.set("最近のイベント: —")
            return

        lines: list[str] = []
        for entry in list(recent)[-4:][::-1]:
            finished_at = entry.get("finished_at", "")
            try:
                dt = datetime.fromisoformat(finished_at)
                time_text = dt.astimezone().strftime("%H:%M")
            except Exception:
                time_text = "--:--"

            result_text = "完了" if entry.get("result") == "success" else "失敗"
            detail = entry.get("error") or entry.get("published_note") or entry.get("note_path") or ""
            if detail:
                detail = f"  /  {Path(str(detail)).name}"
            lines.append(f"{time_text}  {result_text}: {entry.get('file', '')}{detail}")

        self._pipeline_events_var.set("最近のイベント:\n" + "\n".join(lines))

    def _update_pipeline_recent_count(self, recent: list) -> None:
        """直近 24 時間の成功/失敗件数 + 現在リトライ中の件数を集計して
        _pipeline_recent_var を更新する。

        recent[] は 24h 内の処理結果（個々の試行）、retry_count.json は
        「現時点でリトライ中」のファイル数を表す。両者は別概念のためラベルに併記する。

        Args:
            recent: pipeline_state.json の "recent" リスト
        """
        now_ts = datetime.now(timezone.utc)
        success_count = 0
        fail_count = 0
        for entry in recent:
            finished_at_str = entry.get("finished_at", "")
            try:
                # ISO8601 文字列をパース
                finished_at = datetime.fromisoformat(finished_at_str)
                if finished_at.tzinfo is None:
                    finished_at = finished_at.replace(tzinfo=timezone.utc)
                diff_hours = (now_ts - finished_at).total_seconds() / 3600
                if diff_hours <= 24:
                    if entry.get("result") == "success":
                        success_count += 1
                    else:
                        fail_count += 1
            except Exception:
                pass  # パース失敗のエントリは無視

        # retry_count.json からリトライ中の件数を取得（読み込み失敗時は 0）
        retrying_count = self._count_retrying_files()

        self._pipeline_recent_var.set(
            f"最近の処理（直近 24h）: ✓ {success_count} 件 / ✗ {fail_count} 件"
            f"  ／  リトライ中: {retrying_count} 件"
        )

    def _count_retrying_files(self) -> int:
        """retry_count.json を読み込んでリトライ中のファイル数を返す。

        リトライ中 = 失敗したが連続失敗回数が retry_max に達していないファイル。
        ファイル不在 / 壊れた JSON は 0 件として扱う。

        Returns:
            リトライ中ファイル数（0 以上）
        """
        try:
            settings = load_settings()
            rcf_rel = settings.get("pipeline", {}).get(
                "retry_count_file", "data/logs/retry_count.json"
            )
            rcf = resolve_path(rcf_rel)
            input_dirs = [
                resolve_path(settings["paths"]["input_dir"]),
                resolve_path(settings["paths"]["input_text_dir"]),
            ]
            from retry_tracker import count_active_retries
            return count_active_retries(rcf, input_dirs)
        except Exception:
            return 0

    # ------------------------------------------------------------------
    # サービス管理カードの表示更新とボタン操作
    # ------------------------------------------------------------------

    def _create_tooltip(self, widget: Any, text: str) -> None:
        """ウィジェットにマウスホバー tooltip を付与する（インスタンスメソッド版）。

        tkinter には標準 tooltip がないため、<Enter> / <Leave> イベントで
        Toplevel ウィンドウを出し入れする実装。

        クロージャではなく self._tooltip_windows（dict）で状態を管理することで、
        「100ms ごとに unbind/rebind しても古い Toplevel が destroy されないままリーク
        し続ける」問題を解消する。id(widget) をキーにするため、同じ widget に対する
        tooltip は最大 1 つだけ存在が保証される。

        Args:
            widget: tooltip を付けたいウィジェット
            text:   tooltip に表示する文字列
        """
        widget_id = id(widget)

        def _show(event: Any) -> None:
            """マウスが widget に乗ったとき: 既存 Toplevel を destroy してから新規作成。"""
            # 既存 Toplevel があれば先に破棄（多重生成防止）
            existing = self._tooltip_windows.get(widget_id)
            if existing is not None:
                try:
                    existing.destroy()
                except Exception:
                    pass
            tip = tk.Toplevel(self.root)
            tip.wm_overrideredirect(True)   # タイトルバーを消す
            # widget の真下に表示（マウス座標ベースより安定する）
            x = widget.winfo_rootx() + 20
            y = widget.winfo_rooty() + widget.winfo_height() + 5
            tip.wm_geometry(f"+{x}+{y}")
            tip.attributes("-topmost", True)
            tk.Label(
                tip,
                text=text,
                justify="left",
                background="#ffffcc",
                relief="solid",
                borderwidth=1,
                font=("", 9),
                padx=6,
                pady=4,
            ).pack()
            self._tooltip_windows[widget_id] = tip

        def _hide(event: Any) -> None:
            """マウスが widget から離れたとき: Toplevel を破棄して dict から削除。"""
            existing = self._tooltip_windows.pop(widget_id, None)
            if existing is not None:
                try:
                    existing.destroy()
                except Exception:
                    pass

        widget.bind("<Enter>", _show)
        widget.bind("<Leave>", _hide)

    def _set_tooltip(self, widget: Any, text: str) -> None:
        """tooltip を安全に付け替えるラッパー（インスタンスメソッド版）。

        毎回 <Enter> / <Leave> の binding を unbind してからセットし直すことで、
        状態遷移後に古い tooltip が残留する問題を防ぐ。
        text が空文字の場合は binding を外し、既存 Toplevel も destroy する。

        注意: <Enter> / <Leave> のみを操作し、クリック等の他の binding は維持する。

        Args:
            widget: tooltip を設定したいウィジェット
            text:   tooltip に表示する文字列。空文字を渡すと tooltip を削除する。
        """
        # 既存の Enter/Leave binding を解除（他の binding は維持）
        widget.unbind("<Enter>")
        widget.unbind("<Leave>")
        # text が空文字のときは既存 Toplevel も確実に消す
        widget_id = id(widget)
        existing = self._tooltip_windows.pop(widget_id, None)
        if existing is not None:
            try:
                existing.destroy()
            except Exception:
                pass
        # text が指定されている場合のみ新規 binding を付ける
        if text:
            self._create_tooltip(widget, text)

    def _update_service_tab(self, cache: dict[str, ServiceInfo]) -> None:
        """サービス管理カードの各行ウィジェットをキャッシュの内容で更新する。

        この関数は _tick() から main スレッドで呼ばれる。
        ウィジェットが未作成の場合（起動直後）は何もしない。

        Args:
            cache: _service_status_cache のスナップショット。キー = サービス名。
        """
        for name, info in cache.items():
            widgets = self._service_widgets.get(name)
            if widgets is None:
                continue

            status_label: ttk.Label = widgets["status_label"]
            detail_label: ttk.Label = widgets["detail_label"]
            btn: ttk.Button = widgets["button"]

            if info.status == ServiceStatus.RUNNING:
                # 稼働中: 緑のインジケータ
                status_label.config(text="● 稼働中", foreground="green")
                detail_label.config(text=info.detail, foreground="#333")
                # 外部起動でも PID を検出できるものは停止可能にする。
                btn.config(text="停止", state="normal")
                if name in self.service_manager._processes:
                    self._set_tooltip(btn, "")
                else:
                    self._set_tooltip(
                        btn,
                        "外部起動のプロセスです。検出した PID を taskkill で停止します。",
                    )
            elif info.status == ServiceStatus.UNKNOWN:
                # 起動中または HTTP 応答待ち: プロセスはあるので停止操作は許可する（H1）。
                # Open WebUI は HTTP 応答まで時間がかかるため、ユーザーに待機を促す文言を追加する。
                if name == "Open WebUI":
                    status_label.config(text="◐ 起動中（初回は30〜60秒かかります）", foreground="#b36b00")
                    # detail が service_manager から届いていればそれを表示、なければ補足メッセージを出す
                    detail_text = info.detail if info.detail else "HTTP 応答待ち中です。しばらくお待ちください…"
                    detail_label.config(text=detail_text, foreground="#7a4a00")
                else:
                    status_label.config(text="◐ 起動中", foreground="#b36b00")
                    detail_label.config(text=info.detail, foreground="#7a4a00")
                btn.config(text="停止", state="normal")
                self._set_tooltip(
                    btn,
                    "起動中のプロセスです。必要なら検出した PID を taskkill で停止します。",
                )
            else:
                # 停止中: グレーのインジケータ
                status_label.config(text="○ 停止中", foreground="#888")
                detail_label.config(text=info.detail, foreground="#aaa")
                btn.config(text="起動", state="normal")
                # 「起動」状態では tooltip 不要 → 既存 binding を外す
                self._set_tooltip(btn, "")

    def _on_service_button(self, name: str) -> None:
        """サービス行の個別ボタン（「起動」または「停止」）が押されたときの処理。

        状態に応じて start_xxx() または stop_service() を呼ぶ。
        起動は時間がかかる可能性があるため、別スレッドで実行する。
        ボタンを disabled にして再連打を防ぐ。
        """
        widgets = self._service_widgets.get(name)
        if widgets is None:
            return

        btn: ttk.Button = widgets["button"]
        current_text = btn.cget("text")

        if current_text == "起動":
            # ボタンを一時的に無効化
            btn.config(state="disabled", text="起動中...")
            # 別スレッドで起動処理（起動完了後にボタンを復帰）
            threading.Thread(
                target=self._start_service_thread,
                args=(name,),
                daemon=True,
            ).start()
        elif current_text == "停止":
            btn.config(state="disabled", text="停止中...")
            threading.Thread(
                target=self._stop_service_thread,
                args=(name,),
                daemon=True,
            ).start()

    def _start_service_thread(self, name: str) -> None:
        """サービス起動をバックグラウンドスレッドで実行する。

        完了後 messagebox で結果を表示する。
        次のポーリングサイクルで状態ラベルが自然に更新される。
        """
        # 名前に応じた起動メソッドを選択
        start_methods = {
            "Ollama": self.service_manager.start_ollama,
            "Pipeline": self.service_manager.start_pipeline,
            "Open WebUI": self.service_manager.start_open_webui,
        }
        method = start_methods.get(name)
        if method is None:
            return

        ok, msg = method()

        # ボタン状態は次のポーリングで _update_service_tab が更新するため、
        # ここでは "起動" に戻しておくだけでよい（失敗時は「起動」に戻す）
        widgets = self._service_widgets.get(name)
        if widgets:
            # main スレッドに戻して UI を更新（after(0, ...) で即キュー）
            if ok:
                self.root.after(0, lambda: widgets["button"].config(text="停止", state="normal"))
            else:
                self.root.after(0, lambda: widgets["button"].config(text="起動", state="normal"))

        # 失敗時のみ messagebox（成功はポーリングで自然に反映される）
        if not ok:
            self.root.after(0, lambda: messagebox.showerror(f"{name} 起動失敗", msg))

    def _stop_service_thread(self, name: str) -> None:
        """サービス停止をバックグラウンドスレッドで実行する。"""
        ok, msg = self.service_manager.stop_service(name)

        widgets = self._service_widgets.get(name)
        if widgets:
            # 停止後は「起動」ボタンに戻す（成功・失敗を問わず）
            self.root.after(0, lambda: widgets["button"].config(text="起動", state="normal"))

        if not ok:
            self.root.after(0, lambda: messagebox.showerror(f"{name} 停止失敗", msg))

    def _on_start_all_services(self) -> None:
        """「すべて起動」ボタン: 3 サービスをまとめて起動する。

        連打防止: _all_services_in_progress フラグが True の間は何もしない。
        フラグが False の場合のみスレッドを起動し、両ボタンを disabled にする。
        """
        # フラグが True = すでに別の一括操作が走っている → 無視
        if self._all_services_in_progress:
            return

        # 誤爆防止: 操作前に確認ダイアログを表示する（M4）
        confirmed = messagebox.askyesno(
            "すべてのサービスを起動しますか？",
            "Ollama・Pipeline・Open WebUI の 3 つのサービスをまとめて起動します。\n\n"
            "起動には時間がかかる場合があります（特に Open WebUI は初回 30〜60 秒ほど）。\n\n"
            "続けますか？",
        )
        if not confirmed:
            return

        # フラグを立てて、両ボタンを無効化（main スレッドなので直接操作可）
        self._all_services_in_progress = True
        if self._start_all_btn:
            self._start_all_btn.config(state="disabled")
        if self._stop_all_btn:
            self._stop_all_btn.config(state="disabled")

        threading.Thread(
            target=self._start_all_services_thread,
            daemon=True,
        ).start()

    def _start_all_services_thread(self) -> None:
        """すべてのサービス起動をバックグラウンドで実行する。

        try/finally で確実にフラグを False に戻し、ボタンを再有効化する。
        """
        try:
            results = self.service_manager.start_all()
            # 失敗したサービスのメッセージだけ表示
            failed = {name: msg for name, (ok, msg) in results.items() if not ok}
            if failed:
                detail = "\n".join(f"・{name}: {msg}" for name, msg in failed.items())
                self.root.after(
                    0,
                    lambda: messagebox.showwarning(
                        "一部のサービス起動に失敗",
                        f"以下のサービスが起動できませんでした:\n\n{detail}"
                    ),
                )
        except Exception as exc:
            logger.warning(f"すべて起動スレッド例外: {exc}")
        finally:
            # 例外発生時も含めて必ずフラグを False に戻す
            self._all_services_in_progress = False
            self.root.after(0, self._restore_all_service_buttons)

    def _on_stop_all_services(self) -> None:
        """「すべて停止」ボタン: 自分が起動した全サービスを停止する。

        連打防止: _all_services_in_progress フラグが True の間は何もしない。
        """
        # フラグが True = すでに別の一括操作が走っている → 無視
        if self._all_services_in_progress:
            return

        # 誤爆防止: 停止は影響が大きいため、確認ダイアログを表示する（M4）
        confirmed = messagebox.askyesno(
            "すべてのサービスを停止しますか？",
            "Ollama・Pipeline・Open WebUI の 3 つのサービスをまとめて停止します。\n\n"
            "⚠ 注意: Pipeline が処理中（文字起こし・要約など）の場合、\n"
            "その処理も中断されます。\n\n"
            "続けますか？",
        )
        if not confirmed:
            return

        # フラグを立てて、両ボタンを無効化
        self._all_services_in_progress = True
        if self._start_all_btn:
            self._start_all_btn.config(state="disabled")
        if self._stop_all_btn:
            self._stop_all_btn.config(state="disabled")

        threading.Thread(
            target=self._stop_all_services_thread,
            daemon=True,
        ).start()

    def _stop_all_services_thread(self) -> None:
        """すべてのサービス停止をバックグラウンドで実行する。

        try/finally で確実にフラグを False に戻し、ボタンを再有効化する。
        """
        try:
            results = self.service_manager.stop_all()
            if not results:
                self.root.after(
                    0,
                    lambda: messagebox.showinfo(
                        "停止",
                        "稼働中として検出できるサービスはありません。"
                    ),
                )
                return
            failed = {name: msg for name, (ok, msg) in results.items() if not ok}
            if failed:
                detail = "\n".join(f"・{name}: {msg}" for name, msg in failed.items())
                self.root.after(
                    0,
                    lambda: messagebox.showwarning(
                        "一部のサービス停止に失敗",
                        f"以下のサービスが停止できませんでした:\n\n{detail}"
                    ),
                )
        except Exception as exc:
            logger.warning(f"すべて停止スレッド例外: {exc}")
        finally:
            # 例外発生時も含めて必ずフラグを False に戻す
            self._all_services_in_progress = False
            self.root.after(0, self._restore_all_service_buttons)

    def _restore_all_service_buttons(self) -> None:
        """「すべて起動」「すべて停止」ボタンを再有効化する（main スレッドから呼ぶ）。"""
        if self._start_all_btn:
            self._start_all_btn.config(state="normal")
        if self._stop_all_btn:
            self._stop_all_btn.config(state="normal")

    # ------------------------------------------------------------------
    # ノートビューア起動（フェーズ D）
    # ------------------------------------------------------------------

    def _open_note_viewer(self) -> None:
        """ノートビューア（note_viewer.py）を別プロセスで起動する。

        既にビューアが起動済みかの判定は行わない（複数ウィンドウが開いても害がないため）。
        Windows 専用: pythonw.exe を使ってコンソールなしで起動する。
        """
        import subprocess
        viewer_script = PROJECT_ROOT / "scripts" / "note_viewer.py"
        pythonw = PROJECT_ROOT / ".venv" / "Scripts" / "pythonw.exe"

        if not viewer_script.exists():
            # 初心者向け: ファイルが見つからない原因と対処法を案内する
            messagebox.showerror(
                "ノートビューアが見つかりません",
                f"ノートビューアのスクリプトファイルが見つかりませんでした。\n\n"
                "【確認してほしいこと】\n"
                "・PersonalRAG を正しくセットアップしたか確認してください。\n"
                "・scripts フォルダに note_viewer.py が存在するか確認してください。\n\n"
                f"（探したパス: {viewer_script}）",
            )
            return

        if not pythonw.exists():
            # 初心者向け: 仮想環境のセットアップ方法を案内する
            messagebox.showerror(
                "Python仮想環境が見つかりません",
                f"仮想環境（.venv）が見つかりませんでした。\n\n"
                "【確認してほしいこと】\n"
                "・README の手順に従って仮想環境をセットアップしてください。\n"
                "  （例: python -m venv .venv → .venv\\Scripts\\activate → pip install -r requirements.txt）\n\n"
                f"（探したパス: {pythonw}）",
            )
            return

        try:
            subprocess.Popen(
                [str(pythonw), str(viewer_script)],
                # 親プロセスが終了してもビューアが動き続けるよう、デタッチ起動する
                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        except Exception as exc:
            messagebox.showerror(
                "ノートビューアを起動できませんでした",
                f"ノートビューアの起動中にエラーが発生しました。\n\n"
                "アプリを再起動してもう一度お試しください。\n\n"
                f"（詳細: {exc}）",
            )

    def _on_change_recordings_dir(self) -> None:
        """「保存先変更」ボタン: フォルダ選択ダイアログで録音保存先を変更する。

        選択されたパスを settings.yaml の paths.recordings_dir に書き戻す。
        書き戻し後は GUI の再起動を促す（即時反映はしない）。

        安全策:
            - 書き戻し前に settings.yaml.bak を自動作成
            - コメント消失を事前に警告
            - 書き戻し失敗時は .bak から自動復元
            - 同じパスが選択された場合は何もしない
        """
        # --- フォルダ選択ダイアログ ---
        initial_dir = str(self.recordings_dir) if self.recordings_dir.exists() else str(PROJECT_ROOT)
        chosen = filedialog.askdirectory(
            title="録音保存先フォルダを選択",
            initialdir=initial_dir,
            mustexist=False,  # 存在しないフォルダも選択可能（後でチェックする）
        )
        if not chosen:
            # キャンセル or ダイアログを × で閉じた
            return

        # --- パスオブジェクトに変換 ---
        chosen_path = Path(chosen)

        # 同じパスが選択された場合は何もしない（バックアップも作らない）
        try:
            if chosen_path.resolve() == self.recordings_dir.resolve():
                return
        except Exception:
            pass  # resolve() 失敗（ネットワークパス等）は無視して続行

        # --- パスの存在チェック ---
        if not chosen_path.exists():
            # フォルダが存在しない場合は作成を確認する
            should_create = messagebox.askyesno(
                "フォルダ作成の確認",
                f"フォルダが存在しません:\n{chosen_path}\n\n作成しますか？",
            )
            if should_create:
                try:
                    chosen_path.mkdir(parents=True, exist_ok=True)
                except Exception as exc:
                    messagebox.showerror(
                        "フォルダ作成エラー",
                        f"フォルダを作成できませんでした:\n{exc}",
                    )
                    return
            else:
                return

        # ネットワークパス（UNC: \\server\share）の疎通確認
        chosen_str = str(chosen_path)
        if chosen_str.startswith("\\\\") or chosen_str.startswith("//"):
            try:
                # 疎通確認: パスが列挙できるか試みる（タイムアウトなし → 数秒かかる場合あり）
                list(chosen_path.iterdir())
            except Exception as exc:
                # 警告を出すが、保存は許可する（NAS が一時的に落ちている可能性もあるため）
                should_continue = messagebox.askyesno(
                    "ネットワークパスの警告",
                    f"ネットワークパスにアクセスできませんでした:\n{exc}\n\n"
                    "このまま保存先として設定しますか？\n"
                    "（NAS が起動していないと録音を保存できません）",
                )
                if not should_continue:
                    return

        # --- コメント消失の警告 + 書き戻し確認 ---
        should_write = messagebox.askyesno(
            "settings.yaml の書き換え確認",
            f"録音保存先を以下のパスに変更します:\n\n{chosen_path}\n\n"
            "【注意】settings.yaml のコメントは書き戻し後に消えます。\n"
            "変更前のファイルは settings.yaml.bak に保存されます。\n\n"
            "続行しますか？",
        )
        if not should_write:
            return

        # --- settings.yaml に書き戻す ---
        # Windows パスは区切り文字を統一して保存（YAML に書くのでスラッシュ or バックスラッシュどちらでも動く）
        save_value = chosen_str

        try:
            update_settings_path(["paths", "recordings_dir"], save_value)
        except Exception as exc:
            messagebox.showerror(
                "設定保存エラー",
                f"settings.yaml の書き込みに失敗しました:\n{exc}\n\n"
                "settings.yaml.bak が存在する場合はそこから手動で復元できます。",
            )
            return

        # --- 変更完了 → 再起動を促す ---
        messagebox.showinfo(
            "保存先を変更しました",
            f"録音保存先を変更しました:\n{chosen_path}\n\n"
            "変更を反映するには GUI を再起動してください。\n"
            "（現在のセッション中は元のフォルダに保存されます）",
        )

    def _update_failed_count_label(self) -> None:
        """failed_files.json を読んで失敗件数を取得し、ボタンラベルを更新する。

        ファイルが存在しない・壊れている場合は 0 件として扱う。
        件数 0 のときはボタンを非活性、1 件以上のときは赤字で強調する。
        """
        if self._failed_files_btn is None:
            return

        count = 0
        try:
            if self._failed_files_log.exists():
                text = self._failed_files_log.read_text(encoding="utf-8")
                data = json.loads(text)
                if isinstance(data, list):
                    count = len(data)
        except Exception:
            # 読み込みエラーは無視して 0 件として扱う
            count = 0

        self._failed_count = count
        label = f"隔離ファイル ({count})"
        if count == 0:
            # 件数 0: ボタンを無効化（押しても何もない状態なので混乱を防ぐ）
            self._failed_files_btn.config(text=label, state="disabled")
        else:
            # 件数 1 以上: 赤字（foreground は ttk.Button では効かないため state="normal" のみ）
            self._failed_files_btn.config(text=label, state="normal")

    def _show_failed_files_dialog(self) -> None:
        """隔離された失敗ファイルの一覧ダイアログを Toplevel で表示する。

        各ファイルに対して「再試行」「削除」「エクスプローラで開く」操作を提供する。

        再試行: failed/ から data/input/ に戻し、retry_count.json / failed_files.json
                からエントリを削除する → pipeline.py が自動で拾って再処理する。
        削除:   確認ダイアログ後に物理削除し、failed_files.json からエントリを削除する。
        エクスプローラで開く: os.startfile(failed_dir) で failed フォルダを開く。
        """
        # failed_files.json を読み込む
        history: list[dict] = []
        try:
            if self._failed_files_log.exists():
                text = self._failed_files_log.read_text(encoding="utf-8")
                parsed = json.loads(text)
                if isinstance(parsed, list):
                    history = parsed
        except Exception as e:
            # 初心者向け: ログファイルが壊れている場合の対処法を案内する
            messagebox.showerror(
                "隔離ファイル一覧を読み込めませんでした",
                f"処理失敗ファイルの記録（failed_files.json）を読み込めませんでした。\n\n"
                "【確認してほしいこと】\n"
                "・NAS（ネットワークドライブ）に接続中か確認してください。\n"
                "・ファイルが壊れている場合は data/logs/failed_files.json を削除してください（削除すると一覧がリセットされます）。\n\n"
                f"（詳細: {e}）",
            )
            return

        # Toplevel ダイアログを作成
        dialog = tk.Toplevel(self.root)
        dialog.title("隔離された失敗ファイル")
        dialog.geometry("700x400")
        dialog.transient(self.root)

        # 説明ラベル
        ttk.Label(
            dialog,
            text=(
                "処理に連続失敗したファイルの一覧です。"
                "各ファイルを再試行するか、不要なら削除してください。"
            ),
            font=("", 9),
            foreground="#666",
            padding=(8, 6),
        ).pack(fill="x")

        # Treeview でファイル一覧を表示
        columns = ("ファイル名", "隔離日時", "失敗回数", "最後のエラー")
        tree = ttk.Treeview(dialog, columns=columns, show="headings", height=10)
        for col, width in zip(columns, [200, 130, 70, 240]):
            tree.heading(col, text=col)
            tree.column(col, width=width, anchor="w")

        # データを挿入（最新順: moved_at 降順で表示）
        # iid は「インデックスベース」(str(idx)) にする。同名ファイルが履歴に複数
        # ある場合に filename 固定だと ID 衝突するため。実体エントリは
        # entry_map[iid] = entry の dict で参照する。
        sorted_history = sorted(
            history,
            key=lambda e: e.get("moved_at", ""),
            reverse=True,
        )
        # iid → entry dict のマッピング。各操作で iid からエントリを引く
        entry_map: dict[str, dict] = {}
        for idx, entry in enumerate(sorted_history):
            iid = str(idx)
            entry_map[iid] = entry
            filename = entry.get("file", "?")
            moved_at = entry.get("moved_at", "")[:19].replace("T", " ")
            errors = entry.get("errors", [])
            fail_count = len(errors)
            last_error = errors[-1] if errors else "—"
            tree.insert("", "end", iid=iid, values=(filename, moved_at, fail_count, last_error))

        scrollbar = ttk.Scrollbar(dialog, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        tree.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=(4, 8))
        scrollbar.pack(side="right", fill="y", pady=(4, 8), padx=(0, 8))

        # --- ボタン行 ---
        btn_frame = ttk.Frame(dialog, padding=(8, 0, 8, 8))
        btn_frame.pack(fill="x", side="bottom")

        def _get_selected() -> tuple[str, dict] | None:
            """Treeview で選択中の行の (iid, entry) を返す。未選択なら None。"""
            selected = tree.selection()
            if not selected:
                messagebox.showinfo("操作", "ファイルを選択してください。", parent=dialog)
                return None
            iid = selected[0]
            entry = entry_map.get(iid)
            if entry is None:
                return None
            return iid, entry

        def _resolve_entry_path(entry: dict) -> tuple[Path, Path] | None:
            """failed_files.json のエントリから (実ファイル絶対パス, 入力フォルダ) を返す。

            セキュリティ: 最終的に返すパスは必ず self._failed_dirs[source_type] の
            **配下** に収まるよう検証する。failed_files.json が壊れている or
            手編集された場合に、failed/ 外の任意パスに対して削除・移動・フォルダ作成が
            走らないようにするための path traversal 防御。

            優先順位:
              1. moved_to（プロジェクトルートからの相対パス、新しいエントリで保証）
                 → resolve 後 failed_dir 配下にあるかチェック、外れたら 2 へフォールバック
              2. moved_to_name（リネーム後ファイル名）
                 → basename 化してから failed_dir / 名前 で安全に組み立て
              3. file（元のファイル名、古いエントリのフォールバック）
                 → 同様に basename 化

            source_type が無い古いエントリは拡張子から推測する。
            """
            # source_type を取得（古いエントリは拡張子から推測）
            source_type = entry.get("source_type")
            if source_type not in ("audio", "text"):
                ext = Path(entry.get("file", "")).suffix.lower()
                audio_exts = {".wav", ".mp3", ".m4a", ".flac", ".ogg"}
                source_type = "audio" if ext in audio_exts else "text"

            failed_dir = self._failed_dirs.get(source_type)
            if failed_dir is None:
                return None
            input_dir = failed_dir.parent  # 親が data/input または data/input_text

            candidate: Path | None = None

            # 優先順位 1: moved_to を試す（ただし failed_dir 配下である検証必須）
            moved_to = entry.get("moved_to")
            if moved_to:
                try:
                    full_path = (PROJECT_ROOT / moved_to).resolve()
                    # failed_dir も resolve して比較（シンボリックリンク等も実体で見る）
                    failed_resolved = failed_dir.resolve()
                    # is_relative_to は Python 3.9+。配下なら採用
                    if full_path.is_relative_to(failed_resolved):
                        candidate = full_path
                    # 配下外なら無視してフォールバックへ
                except (OSError, ValueError):
                    pass

            # 優先順位 2 & 3: moved_to_name → file。basename 化して安全に組み立てる
            # （basename 化により `..` や絶対パス指定を防ぐ）
            if candidate is None:
                raw_name = entry.get("moved_to_name") or entry.get("file", "")
                safe_name = Path(raw_name).name  # ディレクトリ部分を捨てる
                if not safe_name:
                    return None
                candidate = failed_dir / safe_name

            return candidate, input_dir

        def _save_history(new_history: list[dict]) -> None:
            """更新した failed_files.json を保存してボタンラベルを更新する。"""
            try:
                from retry_tracker import _atomic_write
                _atomic_write(self._failed_files_log, new_history, logger)
            except Exception as exc:
                logger.warning(f"failed_files.json の保存失敗: {exc}")
            # ボタンラベルを即時更新
            self._failed_count = len(new_history)
            label = f"隔離ファイル ({self._failed_count})"
            if self._failed_files_btn is not None:
                state = "normal" if self._failed_count > 0 else "disabled"
                self._failed_files_btn.config(text=label, state=state)

        def _on_retry() -> None:
            """「再試行」ボタン: failed/ から元の入力フォルダに戻して再処理を促す。"""
            # nonlocal は関数冒頭で 1 回だけ宣言する（Python の構文制約）
            nonlocal history
            sel = _get_selected()
            if sel is None:
                return
            iid, entry = sel

            resolved = _resolve_entry_path(entry)
            if resolved is None:
                messagebox.showerror(
                    "再試行",
                    "このエントリのファイル種別を判定できませんでした。",
                    parent=dialog,
                )
                return
            failed_path, input_dir = resolved

            # ファイルが failed/ に存在するか確認
            if not failed_path.exists():
                messagebox.showwarning(
                    "再試行",
                    f"隔離先にファイルが見つかりません:\n{failed_path}\n\n"
                    "既に手動で移動・削除された可能性があります。\n"
                    "履歴のみ削除します。",
                    parent=dialog,
                )
                # 履歴掃除（ファイルがないので戻すものはない）
                history = [e for e in history if e is not entry]
                entry_map.pop(iid, None)
                _save_history(history)
                tree.delete(iid)
                return

            # 元の入力フォルダに移動（リネーム後の実ファイル名でそのまま戻す）
            dest = input_dir / failed_path.name
            try:
                import shutil as _shutil
                _shutil.move(str(failed_path), str(dest))
            except Exception as exc:
                messagebox.showerror("再試行失敗", f"ファイルを戻せませんでした:\n{exc}", parent=dialog)
                return

            # .meta.json も戻す（隔離先に並んでいた場合のみ）
            meta_failed = failed_path.parent / (failed_path.stem + ".meta.json")
            if meta_failed.exists():
                try:
                    import shutil as _shutil
                    _shutil.move(str(meta_failed), str(input_dir / meta_failed.name))
                except Exception:
                    pass  # meta.json の移動失敗は致命的でない

            # retry_count.json からエントリを削除（次回処理は 0 からカウント）
            try:
                from retry_tracker import load_retry_state, save_retry_state
                _settings = load_settings()
                _rcf = resolve_path(
                    _settings.get("pipeline", {}).get(
                        "retry_count_file", "data/logs/retry_count.json"
                    )
                )
                state = load_retry_state(_rcf)
                # 元のファイル名と隔離後ファイル名の両方をクリア（用心深く）
                for name in {entry.get("file", ""), failed_path.name}:
                    if name and name in state:
                        del state[name]
                save_retry_state(_rcf, state, logger)
            except Exception as exc:
                logger.warning(f"retry_count.json のクリア失敗（処理は継続）: {exc}")

            # failed_files.json から該当エントリを削除
            history = [e for e in history if e is not entry]
            entry_map.pop(iid, None)
            _save_history(history)
            tree.delete(iid)

            messagebox.showinfo(
                "再試行",
                f"{failed_path.name} を {input_dir.name}/ に戻しました。\n"
                "pipeline.py が次のタイミングで自動処理を開始します。",
                parent=dialog,
            )

        def _on_delete() -> None:
            """「削除」ボタン: 確認後に物理削除し、failed_files.json からエントリを削除する。"""
            sel = _get_selected()
            if sel is None:
                return
            iid, entry = sel

            resolved = _resolve_entry_path(entry)
            if resolved is None:
                messagebox.showerror(
                    "削除",
                    "このエントリのファイル種別を判定できませんでした。",
                    parent=dialog,
                )
                return
            failed_path, _input_dir = resolved

            # 削除確認ダイアログ
            confirmed = messagebox.askyesno(
                "削除の確認",
                f"以下のファイルを完全に削除しますか？\n\n"
                f"{failed_path}\n\n"
                "この操作は元に戻せません。",
                parent=dialog,
            )
            if not confirmed:
                return

            # ファイル存在チェック → 物理削除（unlink は missing_ok=False、デフォルト）
            # missing_ok=True で「対象パスがズレていても成功扱い」になり実体ファイルが
            # 孤立する事故を防ぐため、存在を確認した上で unlink する。
            file_existed = failed_path.exists()
            if file_existed:
                try:
                    failed_path.unlink()  # missing_ok=False（デフォルト）
                except Exception as exc:
                    messagebox.showerror(
                        "削除失敗", f"ファイルを削除できませんでした:\n{exc}", parent=dialog
                    )
                    return
                # .meta.json も削除する（存在チェック付き）
                meta_path = failed_path.parent / (failed_path.stem + ".meta.json")
                if meta_path.exists():
                    try:
                        meta_path.unlink()
                    except Exception:
                        pass  # meta.json の削除失敗は致命的でない
            else:
                # ファイルが見つからない: 履歴のみ削除する
                messagebox.showwarning(
                    "ファイルなし",
                    f"対象ファイルが見つかりません:\n{failed_path}\n\n"
                    "既に手動で削除されているか、パスがズレている可能性があります。\n"
                    "履歴からのみ削除します。",
                    parent=dialog,
                )

            # failed_files.json からエントリを削除
            nonlocal history
            history = [e for e in history if e is not entry]
            entry_map.pop(iid, None)
            _save_history(history)
            tree.delete(iid)

            if file_existed:
                messagebox.showinfo("削除完了", f"{failed_path.name} を削除しました。", parent=dialog)

        def _on_open_folder() -> None:
            """「エクスプローラで開く」ボタン: failed/ フォルダをエクスプローラで開く。

            選択行があれば、その source_type に応じた failed/ フォルダを開く。
            未選択時は音声側 (data/input/failed) をデフォルトで開く。
            """
            target_dir: Path | None = None
            sel = tree.selection()
            if sel:
                entry = entry_map.get(sel[0])
                if entry is not None:
                    resolved = _resolve_entry_path(entry)
                    if resolved is not None:
                        # resolved の failed_path は実ファイル。親が failed/ フォルダ
                        target_dir = resolved[0].parent
            if target_dir is None:
                target_dir = self._failed_dirs["audio"]

            try:
                target_dir.mkdir(parents=True, exist_ok=True)
                os.startfile(str(target_dir))
            except Exception as exc:
                messagebox.showerror(
                    "エクスプローラ起動失敗",
                    f"フォルダを開けませんでした:\n{exc}",
                    parent=dialog,
                )

        # ボタンを横並びに配置
        ttk.Button(btn_frame, text="再試行", command=_on_retry, width=10).pack(
            side="left", padx=(0, 6)
        )
        ttk.Button(btn_frame, text="削除", command=_on_delete, width=10).pack(
            side="left", padx=(0, 6)
        )
        ttk.Button(
            btn_frame, text="エクスプローラで開く", command=_on_open_folder, width=18
        ).pack(side="left")
        ttk.Button(btn_frame, text="閉じる", command=dialog.destroy, width=10).pack(
            side="right"
        )

    def _show_pipeline_detail(self) -> None:
        """パイプライン処理の詳細一覧を Toplevel で表示する。

        recent リストをテーブル形式で表示し、success 行をクリックすると
        os.startfile() でノートを既定アプリで開く。
        """
        try:
            if not self._pipeline_state_file.exists():
                messagebox.showinfo("パイプライン詳細", "状態ファイルが見つかりません。\npipeline.py を起動してください。")
                return

            text = self._pipeline_state_file.read_text(encoding="utf-8")
            data = json.loads(text)
        except Exception as e:
            # 初心者向け: 状態ファイルが壊れている場合の対処法を案内する
            messagebox.showerror(
                "処理状況を読み込めませんでした",
                f"パイプラインの状態ファイルを読み込めませんでした。\n\n"
                "【確認してほしいこと】\n"
                "・pipeline.py が起動中か確認してください。\n"
                "・NAS（ネットワークドライブ）に接続中か確認してください。\n\n"
                f"（詳細: {e}）",
            )
            return

        recent = data.get("recent", [])

        # Toplevel ウィンドウを作成
        detail_win = tk.Toplevel(self.root)
        detail_win.title("パイプライン処理履歴")
        detail_win.geometry("620x320")
        detail_win.transient(self.root)

        ttk.Label(
            detail_win,
            text="行をクリックすると、ノートファイルを既定アプリで開きます（成功行のみ）。",
            font=("", 9),
            foreground="#666",
            padding=(8, 4),
        ).pack(fill="x")

        # テーブル（Treeview）
        columns = ("結果", "ファイル名", "完了時刻", "備考")
        tree = ttk.Treeview(detail_win, columns=columns, show="headings", height=12)
        for col, width in zip(columns, [60, 220, 140, 160]):
            tree.heading(col, text=col)
            tree.column(col, width=width, anchor="w")

        # 最新順で表示（リストは古い順なので逆順）
        note_paths: dict[str, str] = {}  # iid → note_path のマップ
        for entry in reversed(recent):
            result = entry.get("result", "?")
            filename = entry.get("file", "?")
            finished_at = entry.get("finished_at", "")[:19].replace("T", " ")
            if result == "success":
                note = entry.get("published_note") or entry.get("note_path", "")
                note_basename = Path(note).name if note else ""
                iid = tree.insert("", "end", values=("✓", filename, finished_at, note_basename))
                if note:
                    note_paths[iid] = note
            else:
                error = entry.get("error", "不明")
                tree.insert("", "end", values=("✗", filename, finished_at, error))

        # クリックで note を開く
        def _on_click(event: Any) -> None:
            item = tree.identify_row(event.y)
            if item and item in note_paths:
                try:
                    os.startfile(note_paths[item])
                except Exception as exc:
                    messagebox.showerror("エラー", f"ノートを開けませんでした:\n{exc}")

        tree.bind("<Button-1>", _on_click)

        scrollbar = ttk.Scrollbar(detail_win, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        tree.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)
        scrollbar.pack(side="right", fill="y", pady=8, padx=(0, 8))

    # ------------------------------------------------------------------
    # 無音検知の通知ダイアログ（非モーダル）
    # ------------------------------------------------------------------

    def _show_silence_dialog(self) -> None:
        """無音検知時に一度だけ表示する案内。

        Windows のトースト通知（タスクトレイ右下のバルーン）と、
        画面に出ている GUI 用の Toplevel ダイアログを併用する。
        最小化中はトーストだけが見えるかたち。

        Recorder の判定経路（初回検知前 / 検知後の途切れ）に応じてメッセージを
        切り替える。silence_timeout と voice_loss_timeout で意味が違うため
        固定文言だと誤誘導になる。
        """
        # 経路の判定: 既に一度でも声を検知済みか
        # True なら voice_loss_timeout 経路（会議中に声が途切れた）
        # False なら silence_timeout 経路（マイク選択ミス等で最初から無音）
        voice_was_detected = self.recorder.was_voice_detected()

        if voice_was_detected:
            timeout_seconds = int(self.recorder.voice_loss_timeout)
            toast_message = (
                f"{timeout_seconds} 秒以上、音声を検知できていません。"
                "マイクが外れていないか確認してください。"
            )
            dialog_text = (
                f"録音中に音声が途切れて {timeout_seconds} 秒以上経ちました。\n\n"
                "マイクが外れた、ミュートになった、または長時間沈黙が\n"
                "続いている可能性があります。\n"
                "問題なければそのまま録音を続けてください。"
            )
        else:
            timeout_seconds = int(self.recorder.silence_timeout)
            toast_message = (
                f"{timeout_seconds} 秒間音声を検知できません。"
                "マイクの選択や接続を確認してください。"
            )
            dialog_text = (
                f"録音開始から {timeout_seconds} 秒経っても音声を検知できません。\n\n"
                "マイクの選択ミスや差し直しが原因のことが多いです。\n"
                "「録音を停止する」を押してから正しいマイクを選び、\n"
                "もう一度録音開始してください。"
            )

        # 1) トースト通知: 最小化されていても気付ける
        #    winotify が入っていれば Windows 10/11 のシステム標準トーストを使う。
        #    入っていない場合は pystray のレガシー API を試す（環境依存で表示されない
        #    こともあるがフォールバックとして残す）。
        toast_shown = False
        if _WinotifyNotification is not None:
            try:
                toast = _WinotifyNotification(
                    app_id="PersonalRAG 録音",
                    title="PersonalRAG 録音 - 無音検知",
                    msg=toast_message,
                    duration="long",  # 長めに表示してから通知センターに残す
                )
                toast.show()
                toast_shown = True
            except Exception:
                # winotify は内部で PowerShell を呼ぶ等の経路がある。失敗時はフォールバックへ
                pass

        if not toast_shown and self.tray_icon is not None:
            try:
                self.tray_icon.notify(
                    toast_message,
                    "PersonalRAG 録音 - 無音検知",
                )
            except Exception:
                # トースト通知が出せない環境でも続行（赤字表示は出ているので最低限気付ける）
                pass

        # 2) Toplevel ダイアログ: ウィンドウ表示中なら詳細案内を出す
        dialog = tk.Toplevel(self.root)
        dialog.title("音声が入っていません")
        dialog.geometry("420x180")
        dialog.transient(self.root)
        # モーダルにしない（録音継続中なので操作をブロックしない）

        ttk.Label(
            dialog,
            text=dialog_text,
            justify="left",
            padding=(16, 12),
        ).pack(fill="x")

        button_frame = ttk.Frame(dialog, padding=(16, 8))
        button_frame.pack(fill="x", side="bottom")

        def _stop_from_dialog() -> None:
            dialog.destroy()
            self.command_queue.put(COMMAND_TOGGLE)

        ttk.Button(
            button_frame, text="録音を停止する", command=_stop_from_dialog
        ).pack(side="right", padx=(8, 0))
        ttk.Button(
            button_frame, text="閉じる", command=dialog.destroy
        ).pack(side="right")

    # ------------------------------------------------------------------
    # ウィンドウ・終了処理
    # ------------------------------------------------------------------

    def _on_minimize_to_tray(self) -> None:
        """× ボタン: トレイに収納する。トレイが無ければ通常の終了確認に回す。"""
        if self.tray_icon is not None:
            self.root.withdraw()
        else:
            # トレイ機能が使えない環境（pystray/PIL 未インストール）では完全終了
            self._on_quit_request()

    def _restore_window(self) -> None:
        """トレイ「表示」: 隠れたウィンドウを呼び戻す。"""
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _on_quit_request(self) -> None:
        """完全終了処理。録音中なら確認ダイアログを挟む。

        終了時の方針:
        - サービスポーリングスレッドは停止する（GUI が終了するため不要）
        - Pipeline / Open WebUI は GUI 終了後も継続させる（ユーザーが意図せず処理を止めないため）
        - atexit フックは service_manager.cleanup() を保険として登録しているが、
          「自分が起動したサービスを継続させる」ため cleanup() は呼ばない設計
        """
        if self.recorder.is_running():
            should_stop = messagebox.askyesno(
                "確認", "録音中です。停止して終了しますか？"
            )
            if not should_stop:
                return
            try:
                self.recorder.stop()
            except Exception:
                pass

        # サービスポーリングスレッドを停止する
        # （daemon=True なので強制終了でも問題ないが、正常に停止する）
        self._service_poll_stop.set()
        self.service_manager.stop_notes_auto_sync()

        # 終了順序: hotkey スレッド → トレイスレッド → tkinter
        # 順序を間違えると pystray / hotkey スレッドが残ってプロセスが死なないことがある
        if self.hotkey_manager is not None:
            try:
                self.hotkey_manager.stop()
            except Exception:
                pass
        if self.tray_icon is not None:
            try:
                self.tray_icon.stop()
            except Exception:
                pass
        self.root.quit()


def main() -> int:
    """エントリポイント。"""
    try:
        app = RecordingApp()
    except Exception as exc:
        # 起動失敗時はメッセージボックスで通知（pythonw 起動でも見える）
        try:
            tmp = tk.Tk()
            tmp.withdraw()
            # 初心者向け: 起動失敗時の対処法を案内する
            messagebox.showerror(
                "アプリを起動できませんでした",
                f"録音 GUI の起動中にエラーが発生しました。\n\n"
                "【確認してほしいこと】\n"
                "・config/settings.yaml の内容が正しいか確認してください。\n"
                "・仮想環境（.venv）が正しくセットアップされているか確認してください。\n"
                "・README の「セットアップ手順」をもう一度確認してください。\n\n"
                f"（詳細: {exc}）",
            )
            tmp.destroy()
        except Exception:
            print(f"[record_gui] 起動エラー: {exc}", file=sys.stderr)
        return 1

    app.root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
