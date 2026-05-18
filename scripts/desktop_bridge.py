"""Helpers shared by the desktop intake UI and the remote pipeline.

This module intentionally avoids tkinter imports so the behavior can be tested
without starting a GUI.
"""

from __future__ import annotations

import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


INPUT_AUDIO = "audio"
INPUT_TEXT = "text"


def normalize_extensions(values: list[str] | tuple[str, ...] | set[str]) -> set[str]:
    """Return lowercase extensions with a leading dot."""
    normalized: set[str] = set()
    for value in values:
        ext = str(value).strip().lower()
        if not ext:
            continue
        normalized.add(ext if ext.startswith(".") else f".{ext}")
    return normalized


def classify_input_file(
    path: Path,
    audio_extensions: list[str] | tuple[str, ...] | set[str],
    text_extensions: list[str] | tuple[str, ...] | set[str],
) -> str | None:
    """Classify a desktop-dropped file as audio, text, or unsupported."""
    suffix = path.suffix.lower()
    if suffix in normalize_extensions(audio_extensions):
        return INPUT_AUDIO
    if suffix in normalize_extensions(text_extensions):
        return INPUT_TEXT
    return None


def copy_file_unique(source: Path, dest_dir: Path) -> Path:
    """Copy source into dest_dir without overwriting an existing file."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / source.name
    if dest.exists():
        stem, suffix = source.stem, source.suffix
        for index in range(1, 1000):
            candidate = dest_dir / f"{stem}_{index:03d}{suffix}"
            if not candidate.exists():
                dest = candidate
                break
    shutil.copy2(source, dest)
    return dest


def publish_note_copy(note_path: Path, published_notes_dir: Path) -> Path:
    """Publish a generated note for desktop viewing.

    The destination uses the same file name and is atomically replaced so a
    desktop UI never reads a half-written markdown file.
    """
    published_notes_dir.mkdir(parents=True, exist_ok=True)
    dest = published_notes_dir / note_path.name
    tmp = dest.with_name(f"{dest.name}.tmp")
    try:
        shutil.copy2(note_path, tmp)
        os.replace(tmp, dest)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    return dest


def parse_iso_datetime(value: str) -> datetime | None:
    """Parse an ISO timestamp and normalize it to timezone-aware UTC."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def is_pipeline_state_fresh(
    payload: dict[str, Any],
    *,
    now: datetime | None = None,
    stale_seconds: int = 30,
) -> bool:
    """Return True when pipeline_state.json was updated recently enough."""
    updated_at = parse_iso_datetime(str(payload.get("updated_at", "")))
    if updated_at is None:
        return False
    now_utc = now.astimezone(timezone.utc) if now else datetime.now(timezone.utc)
    return (now_utc - updated_at).total_seconds() <= stale_seconds
