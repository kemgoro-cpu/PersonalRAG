from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from desktop_bridge import (
    INPUT_AUDIO,
    INPUT_TEXT,
    classify_input_file,
    copy_file_unique,
    is_pipeline_state_fresh,
    publish_note_copy,
)


def test_classify_input_file_routes_audio_and_text() -> None:
    audio_exts = [".wav", ".mp3"]
    text_exts = [".txt", ".vtt", ".docx", ".md"]

    assert classify_input_file(Path("meeting.WAV"), audio_exts, text_exts) == INPUT_AUDIO
    assert classify_input_file(Path("teams.DOCX"), audio_exts, text_exts) == INPUT_TEXT
    assert classify_input_file(Path("summary.md"), audio_exts, text_exts) == INPUT_TEXT
    assert classify_input_file(Path("image.png"), audio_exts, text_exts) is None


def test_copy_file_unique_does_not_overwrite(tmp_path: Path) -> None:
    source = tmp_path / "source" / "meeting.wav"
    source.parent.mkdir()
    source.write_text("new", encoding="utf-8")
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()
    (dest_dir / "meeting.wav").write_text("old", encoding="utf-8")

    copied = copy_file_unique(source, dest_dir)

    assert copied.name == "meeting_001.wav"
    assert copied.read_text(encoding="utf-8") == "new"
    assert (dest_dir / "meeting.wav").read_text(encoding="utf-8") == "old"


def test_publish_note_copy_replaces_existing_file(tmp_path: Path) -> None:
    note = tmp_path / "note.md"
    note.write_text("# updated", encoding="utf-8")
    published_dir = tmp_path / "summaries"
    published_dir.mkdir()
    (published_dir / "note.md").write_text("# old", encoding="utf-8")

    published = publish_note_copy(note, published_dir)

    assert published == published_dir / "note.md"
    assert published.read_text(encoding="utf-8") == "# updated"


def test_pipeline_state_freshness() -> None:
    now = datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc)
    fresh = {"updated_at": (now - timedelta(seconds=10)).isoformat()}
    stale = {"updated_at": (now - timedelta(seconds=60)).isoformat()}

    assert is_pipeline_state_fresh(fresh, now=now, stale_seconds=30) is True
    assert is_pipeline_state_fresh(stale, now=now, stale_seconds=30) is False
    assert is_pipeline_state_fresh({"updated_at": "not-a-date"}, now=now) is False
