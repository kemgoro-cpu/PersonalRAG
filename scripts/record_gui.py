"""record_gui.py
Step 1（補助）: マイク録音を「ボタン・ホットキー・タスクトレイ」で操作できる GUI。

scripts/record_mic.py の CLI 版と同じ Recorder クラス（scripts/recorder.py）を
使って録音するので、保存先や品質設定は完全に共通。

主な機能:
    - tkinter ウィンドウの大きなトグルボタンで録音 ON/OFF
    - グローバルホットキー (デフォルト Ctrl+Alt+R) でウィンドウ非アクティブでも操作可
    - タスクトレイ常駐アイコン (グレー=待機 / 赤=録音中)、メニューから操作・終了
    - マイクデバイス選択プルダウン (停止中のみ操作可)
    - 5秒間連続で無音を検知したらステータス赤字表示 + 一度だけ通知ダイアログ
    - 保存先は config/settings.yaml の paths.recordings_dir (NAS の UNC パス可)

スレッド構成:
    main          : tkinter mainloop（ウィジェット操作はすべてここから）
    recorder worker: Recorder.start() 内部で起動 (sounddevice + soundfile)
    pystray       : icon.run_detached() で別スレッド
    hotkey thread : win_hotkey.GlobalHotkey (RegisterHotKey + GetMessageW)

すべての操作要求は queue.Queue (command_queue) に集約し、main の _tick() で
取り出して処理する。tkinter ウィジェットは絶対に main スレッド以外から触らない。
"""

from __future__ import annotations

import enum
import queue
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import tkinter as tk
from tkinter import messagebox, ttk

import sounddevice as sd

from config_loader import load_settings, resolve_path
from recorder import Recorder


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

        # --- 画像（pystray 利用可能時のみ生成） ---
        self._gray_image: Any = self._make_icon_image("gray") if Image else None
        self._red_image: Any = self._make_icon_image("red") if Image else None

        # --- GUI 構築 ---
        self.root = tk.Tk()
        self.root.title("PersonalRAG 録音")
        self.root.geometry("520x300")
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
        """tkinter のウィジェット配置。"""
        frame = ttk.Frame(self.root, padding=16)
        frame.pack(fill="both", expand=True)

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

        # ホットキー表示
        ttk.Label(
            frame,
            text=f"ホットキー: {self.hotkey.upper()}（settings.yaml で変更可）",
            foreground="#888",
            font=("", 9),
        ).grid(row=5, column=0, columnspan=2, sticky="w", pady=(8, 0))

        frame.columnconfigure(1, weight=1)

        # ×ボタンの挙動: 完全終了せずトレイへ収納（トレイが無ければ確認の上で完全終了）
        self.root.protocol("WM_DELETE_WINDOW", self._on_minimize_to_tray)

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

        # 3) 表示更新
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

        # 4) 次回呼び出しを予約
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

    def _start_recording(self) -> None:
        """録音開始処理。"""
        # 選択中のデバイスを取得
        idx = self.device_combobox.current()
        if idx < 0 or idx >= len(self.device_index_map):
            idx = 0
        self.selected_device = self.device_index_map[idx]

        # ファイル名とフォルダ作成
        filename = f"rec_{datetime.now().strftime('%Y-%m-%d_%H%M%S')}.wav"
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
        """録音停止処理。"""
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
            # 一度も音声を検知していないので WAV を削除する
            # pipeline.py が空っぽのファイルに無駄な文字起こしを走らせるのを防ぐ
            try:
                saved.unlink(missing_ok=True)
            except Exception:
                # 削除失敗（ファイルロック中など）は致命的ではないので握りつぶす
                pass
            self.status_var.set(f"無音のため削除しました: {saved.name}")
            self.path_var.set(str(self.recordings_dir))
            tray_title = "PersonalRAG 録音（無音破棄）"
        else:
            self.status_var.set(f"保存しました: {saved.name}")
            self.path_var.set(str(saved))

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
        if self.tray_icon is not None:
            try:
                self.tray_icon.notify(
                    toast_message,
                    "PersonalRAG 録音 - 無音検知",
                )
            except Exception:
                # トースト通知が出せない環境 (古い Windows 等) でも続行
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
        """完全終了処理。録音中なら確認ダイアログを挟む。"""
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
