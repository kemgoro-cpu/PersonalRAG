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
from tkinter import messagebox, ttk

import sounddevice as sd

from config_loader import load_settings, resolve_path, PROJECT_ROOT
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

        # 保存先パス（小さく表示）
        ttk.Label(frame, text="保存先:", foreground="#666").grid(row=4, column=0, sticky="w")
        self.path_var = tk.StringVar(value=str(self.recordings_dir))
        ttk.Label(frame, textvariable=self.path_var, foreground="#666").grid(
            row=4, column=1, sticky="w"
        )

        # 「ノートを開く」ボタン（フェーズ D: ノートビューアを起動）
        ttk.Button(
            frame,
            text="📖 ノートを開く",
            command=self._open_note_viewer,
            width=18,
        ).grid(row=5, column=0, columnspan=2, sticky="w", pady=(6, 0))

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
            row=8, column=0, columnspan=2, sticky="w"
        )

        # 現在処理中のファイル表示
        self._pipeline_current_var = tk.StringVar(value="待機中")
        ttk.Label(frame, text="現在:").grid(row=9, column=0, sticky="w", pady=(4, 0))
        ttk.Label(
            frame, textvariable=self._pipeline_current_var, foreground="#333"
        ).grid(row=9, column=1, sticky="w", pady=(4, 0))

        # 直近 24 時間の成功/失敗カウント表示
        self._pipeline_recent_var = tk.StringVar(value="最近の処理: — 件")
        ttk.Label(frame, textvariable=self._pipeline_recent_var, foreground="#555").grid(
            row=10, column=0, columnspan=2, sticky="w", pady=(2, 0)
        )

        # 詳細ボタン（直近の処理一覧を Toplevel で表示）
        ttk.Button(
            frame, text="詳細...", command=self._show_pipeline_detail, width=8
        ).grid(row=11, column=0, columnspan=2, sticky="w", pady=(6, 0))

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

        ファイルが存在しない・JSON パース失敗の場合は「待機中」にフォールバックする。
        ファイル I/O は try/except で握って GUI を止めない。
        """
        try:
            if not self._pipeline_state_file.exists():
                self._pipeline_current_var.set("待機中（pipeline 未起動）")
                self._pipeline_recent_var.set("最近の処理: — 件")
                return

            text = self._pipeline_state_file.read_text(encoding="utf-8")
            data = json.loads(text)

            # 現在処理中の表示
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
                self._pipeline_current_var.set("待機中")

            # 直近 24 時間の成功/失敗カウント
            recent = data.get("recent", [])
            now_ts = datetime.now(timezone.utc)
            success_count = 0
            fail_count = 0
            for entry in recent:
                finished_at_str = entry.get("finished_at", "")
                try:
                    # ISO8601 文字列をパース（Python 3.7+ は fromisoformat 対応）
                    finished_at = datetime.fromisoformat(finished_at_str)
                    # タイムゾーン情報がない場合は UTC とみなす
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

        except Exception:
            # ファイル読み込み失敗・JSON 壊れ等は全て無視してフォールバック
            self._pipeline_current_var.set("待機中")
            self._pipeline_recent_var.set("最近の処理: — 件（状態ファイル読み込み失敗）")

    # ------------------------------------------------------------------
    # サービス管理タブの表示更新とボタン操作
    # ------------------------------------------------------------------

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
                with self._service_status_lock:
                    managed = name in self.service_manager._pids
                if managed:
                    btn.config(text="停止", state="normal")
                else:
                    # 外部で起動されたサービスは停止ボタンをグレーアウト
                    btn.config(text="停止不可", state="disabled")
            else:
                # 停止中: グレーのインジケータ
                status_label.config(text="○ 停止中", foreground="#888")
                detail_label.config(text=info.detail, foreground="#aaa")
                btn.config(text="起動", state="normal")

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
