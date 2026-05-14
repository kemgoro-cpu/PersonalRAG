"""recorder.py
録音ロジックを共通化したモジュール。

scripts/record_mic.py (CLI) と scripts/record_gui.py (GUI) の両方から
import して使う。これにより「録音の挙動」は1箇所に集約され、
GUI とコマンドラインで動作が食い違うことがない。

機能:
    - sounddevice + soundfile による WAV 逐次書き込み
    - 別スレッドの worker で動作（呼び出し側 UI をブロックしない）
    - 無音検知（GUI でマイク選択ミス等を警告するために使う）
        * 直近のピーク振幅を保持して `is_silent()` で問い合わせ可能

スレッドモデル:
    - start() / stop() は呼び出し元（main スレッド）から1スレッドで呼ぶ前提
    - sounddevice のコールバックは別スレッドから飛んでくるため、
      共有状態はすべて self._lock で保護する
"""

from __future__ import annotations

import queue
import sys
import threading
import time
from pathlib import Path

import numpy as np
import sounddevice as sd
import soundfile as sf


class Recorder:
    """マイク入力を WAV に保存するスレッドセーフな録音クラス。

    使い方:
        rec = Recorder(sample_rate=16000, channels=1)
        rec.start(Path("out.wav"))   # 非同期で録音開始
        ...任意の処理...
        rec.stop()                    # 録音停止＆ファイルを閉じる
    """

    def __init__(
        self,
        sample_rate: int,
        channels: int,
        silence_threshold: float = 0.001,
        silence_timeout: float = 10.0,
        voice_loss_timeout: float = 60.0,
        warmup_seconds: float = 2.0,
    ) -> None:
        """Recorder を初期化する。

        Args:
            sample_rate: サンプリングレート (Hz)。whisper は 16000 推奨。
            channels: チャンネル数 (1=モノラル)。
            silence_threshold: 無音判定の振幅しきい値 (float32, 0.0-1.0)。
                マイク入力の最大振幅がこの値を下回り続けると「無音」と判定する。
            silence_timeout: 録音開始から最初の音声検知までの許容秒数。
                一度も音声を拾えていない状態でこの秒数が経つと is_silent() が True を返す。
                マイク選択ミスの早期検知が目的。デフォルト 10 秒。
            voice_loss_timeout: 直近の音声検知から次の音声検知までの許容秒数。
                一度でも声を拾った後、この秒数だけ無音が続くと is_silent() が True を返す。
                会議中の長い沈黙を許容するため、silence_timeout より大きい値を推奨。
                デフォルト 60 秒。
            warmup_seconds: 録音開始直後のこの秒数は無音判定を行わない
                (デバイス初期化の揺らぎを無視するため)。
        """
        self.sample_rate = sample_rate
        self.channels = channels
        self.silence_threshold = silence_threshold
        self.silence_timeout = silence_timeout
        self.voice_loss_timeout = voice_loss_timeout
        self.warmup_seconds = warmup_seconds

        # コールバックから受け取るオーディオチャンクを溜める queue
        self._audio_queue: queue.Queue[np.ndarray] = queue.Queue()
        # 停止指示用のイベント
        self._stop_event = threading.Event()
        # 内部状態を守るためのロック
        self._lock = threading.Lock()
        # 録音用 worker スレッド
        self._worker: threading.Thread | None = None

        # 状態フラグ
        self._running = False
        self._output_path: Path | None = None
        self._error: Exception | None = None
        self._started_at: float | None = None      # 録音開始時刻 (monotonic)
        self._last_voice_at: float | None = None   # 直近で音声を検知した時刻
        self._peak_level = 0.0                     # 直近バッファのピーク振幅

    def start(self, output_path: Path, device: int | None = None) -> None:
        """録音を開始する (非同期)。

        Args:
            output_path: 保存先 WAV ファイルパス。親フォルダは自動作成される。
            device: 入力デバイスのインデックス。None なら OS の既定デバイスを使う。

        Raises:
            RuntimeError: 既に録音中だった場合。
        """
        with self._lock:
            if self._running:
                raise RuntimeError("既に録音中です。stop() を呼んでから再度 start してください。")

            # 状態初期化
            self._output_path = output_path
            self._error = None
            self._started_at = time.monotonic()
            self._last_voice_at = None
            self._peak_level = 0.0
            self._stop_event.clear()

            # 前回録音時の取りこぼしバッファが残っていれば捨てる
            while not self._audio_queue.empty():
                try:
                    self._audio_queue.get_nowait()
                except queue.Empty:
                    break

            self._running = True
            self._worker = threading.Thread(
                target=self._record_worker,
                args=(output_path, device),
                daemon=True,
            )
            self._worker.start()

    def stop(self, timeout: float = 5.0) -> Path | None:
        """録音を停止し、保存先のパスを返す。

        Args:
            timeout: worker スレッドの終了を待つ最大秒数。

        Returns:
            実際に保存された WAV のパス。録音していなかった場合は None または
            最後に保存したパス。

        Raises:
            TimeoutError: worker が timeout 秒以内に終了しなかった場合。
        """
        worker: threading.Thread | None
        with self._lock:
            if not self._running:
                return self._output_path
            worker = self._worker
            self._stop_event.set()

        if worker is not None:
            worker.join(timeout=timeout)

        with self._lock:
            if worker is not None and worker.is_alive():
                err = TimeoutError("録音スレッドの停止がタイムアウトしました。")
                self._error = self._error or err
                raise err
            return self._output_path

    def is_running(self) -> bool:
        """現在録音中かを返す。"""
        with self._lock:
            return self._running

    def elapsed(self) -> float:
        """録音開始からの経過秒数を返す。未開始なら 0.0。"""
        with self._lock:
            started_at = self._started_at
        if started_at is None:
            return 0.0
        return max(0.0, time.monotonic() - started_at)

    def last_error(self) -> Exception | None:
        """コールバックや worker 内で発生した最後の例外を返す (なければ None)。"""
        with self._lock:
            return self._error

    def is_silent(self) -> bool:
        """無音判定を行う。

        判定ロジック:
            - ウォームアップ中 (録音開始から warmup_seconds 秒以内): 常に False
            - まだ一度も音声を検知していない場合:
                  now - started_at >= silence_timeout なら True
                  (= マイクが死んでいるケースの早期検知。デフォルト 10 秒)
            - 一度でも音声を検知済みの場合:
                  now - last_voice_at >= voice_loss_timeout なら True
                  (= 会議中の長い沈黙を許容。デフォルト 60 秒)
        """
        with self._lock:
            if not self._running:
                return False
            started_at = self._started_at
            last_voice_at = self._last_voice_at

        if started_at is None:
            return False

        now = time.monotonic()
        # ウォームアップ中は判定しない（デバイス初期化直後の揺らぎを無視）
        if now - started_at < self.warmup_seconds:
            return False

        if last_voice_at is None:
            # 一度も声を拾えていない → 初回検知用タイムアウトで判定
            return (now - started_at) >= self.silence_timeout
        # 既に一度は声を拾った → 声が途切れた長さで判定
        return (now - last_voice_at) >= self.voice_loss_timeout

    def peak_level(self) -> float:
        """直近のオーディオバッファのピーク振幅 (0.0-1.0) を返す。

        GUI の音量メーター用に使える (今のところ GUI 側で未使用)。
        """
        with self._lock:
            return self._peak_level

    def was_voice_detected(self) -> bool:
        """この録音セッション中に一度でも音声を検知したかを返す。

        warmup_seconds の影響は受けない (warmup 中でも閾値を超えた音があれば True)。
        start() を呼ぶたびに内部の _last_voice_at は None にリセットされるので、
        次のセッションの判定には引き継がれない。

        GUI 側で「一度も音声を拾わなかった録音」を自動削除する判定に使う。
        """
        with self._lock:
            return self._last_voice_at is not None

    # ------------------------------------------------------------------
    # 内部実装
    # ------------------------------------------------------------------

    def _record_worker(self, output_path: Path, device: int | None) -> None:
        """worker スレッドのエントリポイント。WAV を逐次書き込む。"""
        # 保存先フォルダを念のため作成 (start 側でも作っているが二重に)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            with sf.SoundFile(
                str(output_path),
                mode="w",
                samplerate=self.sample_rate,
                channels=self.channels,
                subtype="PCM_16",
            ) as wav_file:
                with sd.InputStream(
                    samplerate=self.sample_rate,
                    channels=self.channels,
                    callback=self._callback,
                    dtype="float32",
                    device=device,
                ):
                    # 停止イベントが来るまで queue から取り出して書き込む
                    while not self._stop_event.is_set():
                        try:
                            chunk = self._audio_queue.get(timeout=0.2)
                            wav_file.write(chunk)
                        except queue.Empty:
                            continue

                    # 残っているバッファを最後まで書き出す
                    while not self._audio_queue.empty():
                        try:
                            wav_file.write(self._audio_queue.get_nowait())
                        except queue.Empty:
                            break
        except Exception as exc:
            # 例外は呼び出し元から last_error() で取得できるよう保存
            with self._lock:
                self._error = exc
        finally:
            with self._lock:
                self._running = False
            self._stop_event.set()

    def _callback(
        self, indata: np.ndarray, frames: int, time_info: object, status: object
    ) -> None:
        """sounddevice から呼ばれるコールバック (別スレッド)。"""
        if status:
            # オーバーラン等の警告。録音は継続させたいので print のみ
            print(f"[mic] status={status}", file=sys.stderr)

        # 入力のピーク振幅を計算 (無音検知・レベル表示用)
        peak = float(np.abs(indata).max()) if indata.size else 0.0
        now = time.monotonic()
        with self._lock:
            self._peak_level = peak
            if peak >= self.silence_threshold:
                # しきい値を超える音が来た時点を「最後に音声を検知した時刻」として記録
                self._last_voice_at = now

        # コールバックは短く終わらせるため、ファイル書き込みは worker に任せる
        self._audio_queue.put(indata.copy())
