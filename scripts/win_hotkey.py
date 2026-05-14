"""win_hotkey.py
Windows 標準 API (RegisterHotKey) を ctypes で叩いてグローバルホットキーを
実装するモジュール。

なぜこれが必要か:
    keyboard ライブラリは低レベルフックを使うが、Windows のフックタイムアウト
    対策やセキュリティ設定で「登録は成功するが反応しない」症状が起きやすい。
    Windows 標準の RegisterHotKey API は OS のメッセージシステムに直接乗るため
    こうした問題が起きにくく、追加 pip 依存もゼロ（ctypes は標準ライブラリ）。

使い方:
    hotkey = GlobalHotkey("ctrl+alt+r", on_pressed=lambda: print("hit"))
    hotkey.start()
    ...任意の処理...
    hotkey.stop()

Windows 専用。macOS/Linux では `is_active()` が False を返すだけ。
"""

from __future__ import annotations

import ctypes
import sys
import threading
from ctypes import wintypes
from typing import Callable


# --- Windows API 定数 ---
# 修飾キー（RegisterHotKey 用）
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008

# メッセージ種別
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012

# ホットキー登録時の ID（このアプリでは 1 つしか登録しないので固定値）
HOTKEY_ID = 1


# 修飾キー名 → 定数の対応表
_MODIFIER_NAMES = {
    "ctrl": MOD_CONTROL,
    "control": MOD_CONTROL,
    "alt": MOD_ALT,
    "shift": MOD_SHIFT,
    "win": MOD_WIN,
    "super": MOD_WIN,
}


def _build_vk_table() -> dict[str, int]:
    """仮想キーコード（Virtual Key Code）の対応表を構築する。

    Windows の VK_* 定数を参考にしている。最小限のキーだけサポート。
    """
    table: dict[str, int] = {}

    # a-z は VK と ASCII が一致する（VK_A = 0x41 = ord('A')）
    for i in range(26):
        table[chr(ord("a") + i)] = 0x41 + i

    # 0-9
    for i in range(10):
        table[str(i)] = 0x30 + i

    # F1-F24
    for i in range(1, 25):
        table[f"f{i}"] = 0x70 + (i - 1)

    # よく使う特殊キー
    table.update(
        {
            "space": 0x20,
            "enter": 0x0D,
            "return": 0x0D,
            "esc": 0x1B,
            "escape": 0x1B,
            "tab": 0x09,
            "backspace": 0x08,
            "delete": 0x2E,
            "insert": 0x2D,
            "home": 0x24,
            "end": 0x23,
            "pageup": 0x21,
            "pagedown": 0x22,
            "left": 0x25,
            "up": 0x26,
            "right": 0x27,
            "down": 0x28,
        }
    )
    return table


_VK_TABLE = _build_vk_table()


def parse_hotkey(spec: str) -> tuple[int, int]:
    """ホットキー文字列を (modifiers, virtual_key) のタプルに変換する。

    Args:
        spec: 例 "ctrl+alt+r" / "ctrl+shift+f12"。大文字小文字・前後空白は無視。

    Returns:
        (modifiers, vk) のタプル。

    Raises:
        ValueError: パース不能な指定だった場合。
    """
    if not spec or not spec.strip():
        raise ValueError("ホットキーが空です")

    parts = [p.strip().lower() for p in spec.split("+") if p.strip()]
    if not parts:
        raise ValueError(f"無効なホットキー: {spec!r}")

    modifiers = 0
    vk: int | None = None

    for part in parts:
        if part in _MODIFIER_NAMES:
            modifiers |= _MODIFIER_NAMES[part]
        elif part in _VK_TABLE:
            if vk is not None:
                raise ValueError(
                    f"ホットキーにメインキーが複数指定されています: {spec!r}"
                )
            vk = _VK_TABLE[part]
        else:
            raise ValueError(f"未対応のキー '{part}' (ホットキー: {spec!r})")

    if vk is None:
        raise ValueError(f"ホットキーにメインキーがありません: {spec!r}")

    return modifiers, vk


class GlobalHotkey:
    """Windows のグローバルホットキーを 1 つ登録・購読するクラス。

    内部で専用スレッドを起こし、`GetMessageW` で WM_HOTKEY を待ち受ける。
    callback はそのスレッドから呼ばれるため、tkinter ウィジェットを直接触らず
    queue.Queue 等にメッセージを積むだけにすること。
    """

    def __init__(self, hotkey_spec: str, callback: Callable[[], None]) -> None:
        """
        Args:
            hotkey_spec: 例 "ctrl+alt+r"。
            callback: ホットキーが押されたときに呼ぶ関数（引数なし）。
                ホットキースレッドから呼ばれるので、tkinter ウィジェットは
                触らず queue 経由で main に流すこと。
        """
        self.spec = hotkey_spec
        # パース時点でエラーがあれば __init__ で例外を出す
        self.modifiers, self.vk = parse_hotkey(hotkey_spec)
        self.callback = callback

        self._thread: threading.Thread | None = None
        self._thread_id: int = 0  # PostThreadMessageW 用
        self._registered = False  # RegisterHotKey 成功フラグ
        self._registered_event = threading.Event()  # 登録完了待ち合わせ
        self._last_error: int = 0  # RegisterHotKey 失敗時のエラーコード

    # ------------------------------------------------------------------
    # 公開 API
    # ------------------------------------------------------------------

    def start(self, timeout: float = 2.0) -> None:
        """ホットキー監視スレッドを起動する。

        Args:
            timeout: RegisterHotKey の結果を待つ最大秒数。
        """
        if sys.platform != "win32":
            # Windows 以外では何もしない（is_active() は False のまま）
            return

        if self._thread is not None and self._thread.is_alive():
            return  # 既に動いている

        self._registered = False
        self._registered_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="GlobalHotkey", daemon=True
        )
        self._thread.start()
        # 登録完了 (または失敗) を待つ
        self._registered_event.wait(timeout=timeout)

    def stop(self, timeout: float = 2.0) -> None:
        """ホットキー監視スレッドを停止する。"""
        if self._thread is None or not self._thread.is_alive():
            return

        if self._thread_id:
            # 監視スレッドの GetMessageW を WM_QUIT で抜けさせる
            try:
                ctypes.windll.user32.PostThreadMessageW(
                    wintypes.DWORD(self._thread_id), WM_QUIT, 0, 0
                )
            except Exception:
                pass

        self._thread.join(timeout=timeout)
        self._thread = None
        self._thread_id = 0
        self._registered = False

    def is_active(self) -> bool:
        """RegisterHotKey が成功し、現在ホットキーが有効かを返す。"""
        return self._registered

    def last_error_code(self) -> int:
        """RegisterHotKey 失敗時の Windows エラーコード。"""
        return self._last_error

    # ------------------------------------------------------------------
    # 内部実装（監視スレッド側）
    # ------------------------------------------------------------------

    def _run(self) -> None:
        """監視スレッドのエントリポイント。"""
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        # PostThreadMessageW で起こすため、自分のスレッド ID を控えておく
        self._thread_id = int(kernel32.GetCurrentThreadId())

        # ホットキー登録（hwnd=None でスレッドキューに直接届く）
        ok = user32.RegisterHotKey(
            None, HOTKEY_ID, self.modifiers, self.vk
        )
        if not ok:
            self._last_error = int(kernel32.GetLastError())
            self._registered = False
            self._registered_event.set()
            return

        self._registered = True
        self._registered_event.set()

        try:
            msg = wintypes.MSG()
            # GetMessageW は次のメッセージが来るまでブロックする
            #   戻り値: > 0 = 通常のメッセージ, 0 = WM_QUIT, -1 = エラー
            while True:
                ret = user32.GetMessageW(
                    ctypes.byref(msg), None, 0, 0
                )
                if ret == 0:
                    # WM_QUIT (stop() で投げられた)
                    break
                if ret == -1:
                    # 想定外エラー。ループを抜ける
                    break
                if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                    try:
                        self.callback()
                    except Exception:
                        # コールバック側のエラーで監視スレッドを死なせない
                        pass
        finally:
            try:
                user32.UnregisterHotKey(None, HOTKEY_ID)
            except Exception:
                pass
            self._registered = False
