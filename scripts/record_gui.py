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
    - サービス管理タブで Ollama / Pipeline / Open WebUI を起動・停止・状態確認

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
from recorder import Recorder
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
        rec_cfg = settings["recording"]
        self.recordings_dir: Path = resolve_path(settings["paths"]["recordings_dir"])
        self.hotkey: str = rec_cfg.get("hotkey", "ctrl+alt+r")

        # --- パイプライン状態ファイルのパス（pipeline.py が書き出すファイルを読む） ---
        state_file_rel = settings.get("pipeline", {}).get(
            "state_file", "data/logs/pipeline_state.json"
        )
        self._pipeline_state_file: Path = resolve_path(state_file_rel)
        # 最後に状態ファイルを読んだ時刻（1 秒スロットリング用）
        self._pipeline_state_last_read: float = 0.0

        # --- 失敗ファイル管理 ---
        # failed_files.json のパス（pipeline.py が書き出す）
        pipeline_cfg = settings.get("pipeline", {})
        self._failed_files_log: Path = resolve_path(
            pipeline_cfg.get("failed_files_log", "data/logs/failed_files.json")
        )
        # 入力フォルダ内の failed/ サブフォルダパス（隔離先）
        self._input_failed_dir: Path = resolve_path(
            settings["paths"]["input_dir"]
        ) / "failed"
        # 最後に失敗件数を読んだ時刻（5 秒スロットリング用）
        self._failed_count_last_read: float = 0.0
        # 現在の隔離済みファイル件数（ボタンラベルに表示）
        self._failed_count: int = 0
        # 「失敗一覧」ボタンのウィジェット参照（_update_failed_count_label で更新）
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
        # daemon=True で GUI 強制終了時にスレッドも自動終了する
        self._service_poll_thread = threading.Thread(
            target=self._poll_services, daemon=True, name="service-poll"
        )
        self._service_poll_thread.start()

        # サービスタブのウィジェット参照（_build_window で設定する）
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

        # --- GUI 構築 ---
        self.root = tk.Tk()
        self.root.title("PersonalRAG 録音")
        # タブ追加に伴い高さを拡張
        self.root.geometry("520x480")
        self.root.resizable(False, False)
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
                    "ホットキー登録失敗", self.hotkey_warning
                ),
            )

    # ------------------------------------------------------------------
    # GUI 構築
    # ------------------------------------------------------------------

    def _build_window(self) -> None:
        """tkinter のウィジェット配置。ttk.Notebook でタブ化する。

        タブ 1「録音」: 既存の録音 UI + パイプライン状態セクション
        タブ 2「サービス管理」: Ollama / Pipeline / Open WebUI の状態と操作
        """
        # Notebook（タブコンテナ）をルートウィンドウに配置
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True, padx=4, pady=4)

        # --- タブ 1: 録音 ---
        recording_tab = ttk.Frame(notebook, padding=16)
        notebook.add(recording_tab, text="録音")
        self._build_recording_tab(recording_tab)

        # --- タブ 2: サービス管理 ---
        service_tab = ttk.Frame(notebook, padding=16)
        notebook.add(service_tab, text="サービス管理")
        self._build_service_tab(service_tab)

        # ×ボタンの挙動: 完全終了せずトレイへ収納（トレイが無ければ確認の上で完全終了）
        self.root.protocol("WM_DELETE_WINDOW", self._on_minimize_to_tray)

    def _build_recording_tab(self, frame: ttk.Frame) -> None:
        """「録音」タブのウィジェットを配置する（既存の録音 UI）。"""
        # マイクデバイス選択
        ttk.Label(frame, text="マイクデバイス").grid(row=0, column=0, sticky="w")
        self.device_var = tk.StringVar()
        self.device_combobox = ttk.Combobox(
            frame, textvariable=self.device_var, state="readonly", width=56
        )
        self.device_combobox.grid(row=1, column=0, columnspan=2, sticky="we", pady=(4, 12))
        self._refresh_devices()

        # トグルボタン（一番目立たせる）
        self.toggle_button = ttk.Button(
            frame,
            text="● 録音開始",
            command=lambda: self.command_queue.put(COMMAND_TOGGLE),
        )
        self.toggle_button.grid(row=2, column=0, columnspan=2, sticky="we", pady=(0, 12))

        # ステータス
        self.status_var = tk.StringVar(value="待機中")
        self.status_label = ttk.Label(frame, textvariable=self.status_var, font=("", 11))
        self.status_label.grid(row=3, column=0, columnspan=2, sticky="w", pady=(0, 4))

        # 保存先パス（小さく表示）＋「📁 変更...」ボタン
        # wraplength=480: ウィンドウ幅 520 から左右の余白を引いた値。
        # 長いパス（NAS の UNC パスや日本語入りパス）が切れずに改行表示される。
        ttk.Label(frame, text="保存先:", foreground="#666").grid(row=4, column=0, sticky="w")
        self.path_var = tk.StringVar(value=str(self.recordings_dir))
        ttk.Label(
            frame, textvariable=self.path_var, foreground="#666", wraplength=480
        ).grid(row=4, column=1, sticky="w")
        # 「📁 変更...」ボタン: フォルダ選択ダイアログで録音保存先を変更する
        ttk.Button(
            frame,
            text="📁 変更...",
            command=self._on_change_recordings_dir,
            width=10,
        ).grid(row=5, column=0, sticky="w", pady=(4, 0))

        # 「ノートを開く」ボタン（フェーズ D: ノートビューアを起動）
        ttk.Button(
            frame,
            text="📖 ノートを開く",
            command=self._open_note_viewer,
            width=18,
        ).grid(row=5, column=1, sticky="w", pady=(4, 0))

        # ホットキー表示
        ttk.Label(
            frame,
            text=f"ホットキー: {self.hotkey.upper()}（settings.yaml で変更可）",
            foreground="#888",
            font=("", 9),
        ).grid(row=6, column=0, columnspan=2, sticky="w", pady=(8, 0))

        # --- パイプライン状態セクション（フェーズ A 成果物をここに残す）---
        ttk.Separator(frame, orient="horizontal").grid(
            row=7, column=0, columnspan=2, sticky="we", pady=(12, 8)
        )
        ttk.Label(frame, text="パイプライン状態", font=("", 10, "bold")).grid(
            row=8, column=0, columnspan=2, sticky="w", pady=(0, 2)
        )

        # 現在処理中のファイル表示
        # _pipeline_current_label: _update_pipeline_status() から foreground を変更するために保持
        self._pipeline_current_var = tk.StringVar(value="待機中")
        ttk.Label(frame, text="現在:").grid(row=9, column=0, sticky="w", pady=(4, 0))
        self._pipeline_current_label = ttk.Label(
            frame, textvariable=self._pipeline_current_var, foreground="#333"
        )
        self._pipeline_current_label.grid(row=9, column=1, sticky="w", pady=(4, 0))

        # 直近 24 時間の成功/失敗カウント表示
        self._pipeline_recent_var = tk.StringVar(value="最近の処理: — 件")
        ttk.Label(frame, textvariable=self._pipeline_recent_var, foreground="#555").grid(
            row=10, column=0, columnspan=2, sticky="w", pady=(2, 0)
        )

        # 詳細ボタン（直近の処理一覧を Toplevel で表示）
        ttk.Button(
            frame, text="詳細...", command=self._show_pipeline_detail, width=8
        ).grid(row=11, column=0, sticky="w", pady=(6, 0))

        # 「失敗一覧」ボタン（隔離済みファイルを一覧・再試行・削除できるダイアログを開く）
        # 件数は _tick() が 5 秒おきに更新する
        self._failed_files_btn = ttk.Button(
            frame,
            text="失敗一覧 (0)",
            command=self._show_failed_files_dialog,
            width=14,
        )
        self._failed_files_btn.grid(row=11, column=1, sticky="w", pady=(6, 0))

        frame.columnconfigure(1, weight=1)

    def _build_service_tab(self, frame: ttk.Frame) -> None:
        """「サービス管理」タブのウィジェットを配置する。

        レイアウト:
            サービス名ラベル + 状態インジケータ（●=稼働 / ○=停止）+ 詳細テキスト + 個別ボタン
            「すべて起動」「すべて停止」ボタン
            VRAM 競合警告ラベル（常時赤字表示）
        """
        # 各サービスの行を構築するヘルパー
        SERVICE_NAMES = ["Ollama", "Pipeline", "Open WebUI"]

        for row_idx, name in enumerate(SERVICE_NAMES):
            # サービス名ラベル
            ttk.Label(frame, text=f"{name}:", width=12, anchor="w").grid(
                row=row_idx, column=0, sticky="w", pady=4
            )
            # 状態インジケータ（● 稼働中 / ○ 停止中）
            status_label = ttk.Label(frame, text="○ 停止中", foreground="#888", width=12)
            status_label.grid(row=row_idx, column=1, sticky="w", pady=4)

            # 詳細テキスト（"稼働中" / "停止中" / "停止中（状態ファイルなし）" 等）
            detail_label = ttk.Label(frame, text="確認中...", foreground="#aaa", width=22)
            detail_label.grid(row=row_idx, column=2, sticky="w", pady=4)

            # 個別操作ボタン（状態に応じて「起動」⇔「停止」を切替）
            # lambda でループ変数をキャプチャするため default 引数で束縛する
            btn = ttk.Button(
                frame,
                text="起動",
                width=8,
                command=lambda n=name: self._on_service_button(n),
            )
            btn.grid(row=row_idx, column=3, sticky="w", padx=(8, 0), pady=4)

            # ウィジェット参照を保存（_update_service_tab で更新するため）
            self._service_widgets[name] = {
                "status_label": status_label,
                "detail_label": detail_label,
                "button": btn,
            }

        # セパレータ
        ttk.Separator(frame, orient="horizontal").grid(
            row=len(SERVICE_NAMES), column=0, columnspan=4,
            sticky="we", pady=(12, 8)
        )

        # 「すべて起動」「すべて停止」ボタン行
        bulk_frame = ttk.Frame(frame)
        bulk_frame.grid(row=len(SERVICE_NAMES) + 1, column=0, columnspan=4, sticky="w")

        # ボタンを self に保持して、連打防止のため disable/enable を後から制御する
        self._start_all_btn = ttk.Button(
            bulk_frame,
            text="すべて起動",
            width=12,
            command=self._on_start_all_services,
        )
        self._start_all_btn.pack(side="left", padx=(0, 8))

        self._stop_all_btn = ttk.Button(
            bulk_frame,
            text="すべて停止",
            width=12,
            command=self._on_stop_all_services,
        )
        self._stop_all_btn.pack(side="left")

        # VRAM 競合警告ラベル（常時赤字）
        ttk.Label(
            frame,
            text=(
                "注意: 文字起こし中の Open WebUI 起動は VRAM 競合の恐れあり。\n"
                "Pipeline 停止後に Open WebUI を起動してください。"
            ),
            foreground="red",
            font=("", 9),
            justify="left",
        ).grid(
            row=len(SERVICE_NAMES) + 2, column=0, columnspan=4,
            sticky="w", pady=(16, 0)
        )

        frame.columnconfigure(2, weight=1)

    def _refresh_devices(self) -> None:
        """入力デバイス一覧を Combobox に流し込む。"""
        try:
            devices = sd.query_devices()
        except Exception as exc:
            messagebox.showerror("デバイス取得エラー", f"マイクデバイス一覧の取得に失敗しました:\n{exc}")
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
                messagebox.showerror("録音エラー", f"録音中に問題が発生しました:\n{err}")

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
                self.status_var.set(f"録音中  {elapsed_str}")
                self.status_label.config(foreground="black")
                if self.tray_icon is not None:
                    self.tray_icon.title = f"PersonalRAG 録音中 {elapsed_str}"

        # 4) パイプライン状態ファイルを読んで GUI を更新（最終読み込みから 1 秒以上経った時のみ）
        now = time.monotonic()
        if now - self._pipeline_state_last_read >= 1.0:
            self._pipeline_state_last_read = now
            self._update_pipeline_status()

        # 4-b) 失敗一覧の件数を 5 秒おきに更新してボタンラベルに反映する
        if now - self._failed_count_last_read >= 5.0:
            self._failed_count_last_read = now
            self._update_failed_count_label()

        # 5) サービス管理タブの表示更新（ロック取得 → キャッシュ参照 → 即解放）
        #    I/O なし・ロック保持時間は数マイクロ秒なので freeze の心配なし
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
            messagebox.showerror(
                "保存先エラー",
                f"保存先フォルダを準備できません:\n{output_path.parent}\n\n{exc}",
            )
            return

        # 録音開始
        try:
            self.recorder.start(output_path, device=self.selected_device)
        except Exception as exc:
            messagebox.showerror("録音開始エラー", f"録音を開始できませんでした:\n{exc}")
            return

        self.current_output = output_path
        self.state = AppState.RECORDING
        self.silence_announced = False

        # UI 更新
        self.toggle_button.config(text="■ 録音停止")
        self.device_combobox.config(state="disabled")
        self.status_var.set("録音中  00:00:00")
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
            messagebox.showerror("録音停止エラー", f"録音停止中に問題が発生しました:\n{exc}")
            saved = self.current_output

        self.state = AppState.IDLE
        self.toggle_button.config(text="● 録音開始")
        self.device_combobox.config(state="readonly")
        self.status_label.config(foreground="black")

        tray_title = "PersonalRAG 録音（待機中）"

        if saved is None:
            # そもそも保存先パスが取れていない（録音開始前の異常停止など）
            self.status_var.set("待機中")
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
            self.status_var.set(f"無音のため削除しました: {saved.name}")
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
            self.status_var.set(f"保存しました: {saved.name}")
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
        self.toggle_button.config(text="● 録音開始")
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
        """pipeline_state.json を読んでパイプライン状態 UI を更新する。

        updated_at が現在時刻から 30 秒以内 → Pipeline 稼働中と判定する。
        30 秒超 / ファイル不在 / パース失敗 → 「Pipeline 停止中」を赤字で表示する。

        service_manager.check_pipeline() と同じ 30 秒しきい値を採用。
        将来的に PIPELINE_FRESH_THRESHOLD_SECONDS として共通定数化できるが、
        最小実装として record_gui.py 内で固定値 30 を使う。
        """
        # Pipeline 稼働判定のしきい値（秒）: service_manager.check_pipeline() と同値
        PIPELINE_FRESH_THRESHOLD_SECONDS = 30

        try:
            if not self._pipeline_state_file.exists():
                # ファイルがない → Pipeline 未起動
                self._pipeline_current_var.set("Pipeline 停止中（状態ファイルなし）")
                self._pipeline_current_label.config(foreground="#cc0000")
                self._pipeline_recent_var.set("最近の処理: —")
                return

            text = self._pipeline_state_file.read_text(encoding="utf-8")
            data = json.loads(text)

            # --- updated_at で Pipeline の死活を判定 ---
            updated_at_str: str = data.get("updated_at", "")
            pipeline_alive = False
            diff_seconds = None

            if updated_at_str:
                try:
                    updated_at = datetime.fromisoformat(updated_at_str)
                    if updated_at.tzinfo is None:
                        updated_at = updated_at.replace(tzinfo=timezone.utc)
                    now_utc = datetime.now(timezone.utc)
                    diff_seconds = (now_utc - updated_at).total_seconds()
                    pipeline_alive = diff_seconds <= PIPELINE_FRESH_THRESHOLD_SECONDS
                except Exception:
                    # updated_at のパース失敗 → 停止とみなす
                    pipeline_alive = False

            if not pipeline_alive:
                # 停止中の表示（赤系で目立たせる）
                if diff_seconds is not None:
                    self._pipeline_current_var.set(
                        f"Pipeline 停止中（最終更新 {int(diff_seconds)}s 前）"
                    )
                else:
                    self._pipeline_current_var.set("Pipeline 停止中（更新時刻不明）")
                self._pipeline_current_label.config(foreground="#cc0000")
                # 停止中でも recent は読めるなら表示する
                recent = data.get("recent", [])
                if recent:
                    self._update_pipeline_recent_count(recent)
                else:
                    self._pipeline_recent_var.set("最近の処理: —")
                return

            # --- Pipeline 稼働中 ---
            self._pipeline_current_label.config(foreground="#333")

            # 現在処理中のファイルを表示
            current = data.get("current")
            if current:
                step_label = {
                    "transcribe": "文字起こし中",
                    "summarize": "要約中",
                    "ingest": "DB 投入中",
                }.get(current.get("step", ""), current.get("step", "処理中"))
                self._pipeline_current_var.set(
                    f"{current.get('file', '')}  ({step_label})"
                )
            else:
                self._pipeline_current_var.set("待機中（pipeline 稼働中）")

            # 直近 24 時間の成功/失敗カウント
            recent = data.get("recent", [])
            self._update_pipeline_recent_count(recent)

        except Exception:
            # ファイル読み込み失敗・JSON 壊れ等は全て無視してフォールバック
            self._pipeline_current_var.set("Pipeline 停止中（状態ファイル読み込み失敗）")
            self._pipeline_current_label.config(foreground="#cc0000")
            self._pipeline_recent_var.set("最近の処理: — （読み込み失敗）")

    def _update_pipeline_recent_count(self, recent: list) -> None:
        """直近 24 時間の成功/失敗件数を集計して _pipeline_recent_var を更新する。

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
        self._pipeline_recent_var.set(
            f"最近の処理（直近 24h）: ✓ {success_count} 件 / ✗ {fail_count} 件"
        )

    # ------------------------------------------------------------------
    # サービス管理タブの表示更新とボタン操作
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
        """サービス管理タブの各行ウィジェットをキャッシュの内容で更新する。

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
                # ボタンを「停止」に切替（ただしこのGUIが起動していない場合はグレーアウト）
                # _processes は ServiceManager 側で内部ロック保護されているため
                # こちら側の _service_status_lock とは独立。読み取りのみなので 'in' は安全
                managed = name in self.service_manager._processes
                if managed:
                    btn.config(text="停止", state="normal")
                    # 「停止」状態では tooltip 不要 → 既存 binding を外す
                    self._set_tooltip(btn, "")
                else:
                    # 外部で起動されたサービスは停止ボタンをグレーアウト＆tooltip で説明
                    # 「停止不可」→「外部起動」に変更してわかりやすくする
                    btn.config(text="外部起動", state="disabled")
                    # _set_tooltip を使うことで状態遷移時に古い binding を確実に外す
                    self._set_tooltip(
                        btn,
                        "この GUI から起動したプロセスではないため停止できません。\n"
                        "タスクマネージャから手動で停止してください。",
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
                        "このGUIから起動したサービスはありません。\n"
                        "外部で起動されたサービスは停止できません。"
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
            messagebox.showerror(
                "エラー",
                f"ノートビューアスクリプトが見つかりません:\n{viewer_script}",
            )
            return

        if not pythonw.exists():
            messagebox.showerror(
                "エラー",
                f"pythonw.exe が見つかりません:\n{pythonw}\n\n"
                "仮想環境が正しくセットアップされているか確認してください。",
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
                "起動エラー",
                f"ノートビューアを起動できませんでした:\n{exc}",
            )

    def _on_change_recordings_dir(self) -> None:
        """「📁 変更...」ボタン: フォルダ選択ダイアログで録音保存先を変更する。

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
        label = f"失敗一覧 ({count})"
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
            messagebox.showerror("失敗一覧", f"failed_files.json の読み込みに失敗しました:\n{e}")
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
        sorted_history = sorted(
            history,
            key=lambda e: e.get("moved_at", ""),
            reverse=True,
        )
        for entry in sorted_history:
            filename = entry.get("file", "?")
            moved_at = entry.get("moved_at", "")[:19].replace("T", " ")
            errors = entry.get("errors", [])
            fail_count = len(errors)
            last_error = errors[-1] if errors else "—"
            tree.insert("", "end", iid=filename, values=(filename, moved_at, fail_count, last_error))

        scrollbar = ttk.Scrollbar(dialog, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        tree.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=(4, 8))
        scrollbar.pack(side="right", fill="y", pady=(4, 8), padx=(0, 8))

        # --- ボタン行 ---
        btn_frame = ttk.Frame(dialog, padding=(8, 0, 8, 8))
        btn_frame.pack(fill="x", side="bottom")

        def _get_selected_entry() -> dict | None:
            """Treeview で選択中の行の失敗エントリを返す。未選択なら None。"""
            selected = tree.selection()
            if not selected:
                messagebox.showinfo("操作", "ファイルを選択してください。", parent=dialog)
                return None
            iid = selected[0]
            # history から該当エントリを探す
            for entry in history:
                if entry.get("file") == iid:
                    return entry
            return None

        def _save_history(new_history: list[dict]) -> None:
            """更新した failed_files.json を保存してボタンラベルを更新する。"""
            try:
                from retry_tracker import _atomic_write
                _atomic_write(self._failed_files_log, new_history, logger)
            except Exception as exc:
                logger.warning(f"failed_files.json の保存失敗: {exc}")
            # ボタンラベルを即時更新
            self._failed_count = len(new_history)
            label = f"失敗一覧 ({self._failed_count})"
            if self._failed_files_btn is not None:
                state = "normal" if self._failed_count > 0 else "disabled"
                self._failed_files_btn.config(text=label, state=state)

        def _on_retry() -> None:
            """「再試行」ボタン: failed/ から data/input/ に戻して再処理を促す。"""
            entry = _get_selected_entry()
            if entry is None:
                return

            filename = entry.get("file", "")
            failed_path = self._input_failed_dir / filename
            input_dir = self._input_failed_dir.parent  # data/input/

            # ファイルが failed/ に存在するか確認
            if not failed_path.exists():
                messagebox.showwarning(
                    "再試行",
                    f"隔離先にファイルが見つかりません:\n{failed_path}\n\n"
                    "既に手動で移動・削除された可能性があります。",
                    parent=dialog,
                )
                return

            # data/input/ に移動
            dest = input_dir / filename
            try:
                import shutil as _shutil
                _shutil.move(str(failed_path), str(dest))
            except Exception as exc:
                messagebox.showerror("再試行失敗", f"ファイルを戻せませんでした:\n{exc}", parent=dialog)
                return

            # .meta.json も戻す
            meta_failed = self._input_failed_dir / (Path(filename).stem + ".meta.json")
            if meta_failed.exists():
                try:
                    import shutil as _shutil
                    _shutil.move(str(meta_failed), str(input_dir / meta_failed.name))
                except Exception:
                    pass  # meta.json の移動失敗は致命的でない

            # retry_count.json からエントリを削除（次回処理は 0 からカウント）
            try:
                from retry_tracker import load_retry_state, save_retry_state
                from config_loader import load_settings, resolve_path as _resolve
                _settings = load_settings()
                _rcf = _resolve(
                    _settings.get("pipeline", {}).get(
                        "retry_count_file", "data/logs/retry_count.json"
                    )
                )
                state = load_retry_state(_rcf)
                if filename in state:
                    del state[filename]
                    save_retry_state(_rcf, state, logger)
            except Exception as exc:
                logger.warning(f"retry_count.json のクリア失敗（処理は継続）: {exc}")

            # failed_files.json からエントリを削除
            nonlocal history
            history = [e for e in history if e.get("file") != filename]
            _save_history(history)

            # Treeview から行を削除
            tree.delete(filename)

            messagebox.showinfo(
                "再試行",
                f"{filename} を data/input/ に戻しました。\n"
                "pipeline.py が次のタイミングで自動処理を開始します。",
                parent=dialog,
            )

        def _on_delete() -> None:
            """「削除」ボタン: 確認後に物理削除し、failed_files.json からエントリを削除する。"""
            entry = _get_selected_entry()
            if entry is None:
                return

            filename = entry.get("file", "")
            failed_path = self._input_failed_dir / filename

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

            # 物理削除
            try:
                failed_path.unlink(missing_ok=True)
            except Exception as exc:
                messagebox.showerror("削除失敗", f"ファイルを削除できませんでした:\n{exc}", parent=dialog)
                return

            # .meta.json も削除する
            meta_path = self._input_failed_dir / (Path(filename).stem + ".meta.json")
            try:
                meta_path.unlink(missing_ok=True)
            except Exception:
                pass  # meta.json の削除失敗は致命的でない

            # failed_files.json からエントリを削除
            nonlocal history
            history = [e for e in history if e.get("file") != filename]
            _save_history(history)

            # Treeview から行を削除
            tree.delete(filename)

            messagebox.showinfo("削除完了", f"{filename} を削除しました。", parent=dialog)

        def _on_open_folder() -> None:
            """「エクスプローラで開く」ボタン: failed/ フォルダをエクスプローラで開く。"""
            try:
                self._input_failed_dir.mkdir(parents=True, exist_ok=True)
                os.startfile(str(self._input_failed_dir))
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
            messagebox.showerror("パイプライン詳細", f"状態ファイルの読み込みに失敗しました:\n{e}")
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
                note = entry.get("note_path", "")
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
            messagebox.showerror("起動エラー", f"録音 GUI を起動できませんでした:\n{exc}")
            tmp.destroy()
        except Exception:
            print(f"[record_gui] 起動エラー: {exc}", file=sys.stderr)
        return 1

    app.root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
