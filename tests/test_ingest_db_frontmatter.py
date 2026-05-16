"""ingest_db.py のフロントマター解析テスト。"""

from __future__ import annotations

import sys
from pathlib import Path


SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from ingest_db import strip_frontmatter


def test_strip_frontmatter_keeps_body_without_frontmatter() -> None:
    """フロントマターがない Markdown は本文をそのまま返す。"""
    text = "# 要約\n\n本文です。"

    meta, body = strip_frontmatter(text)

    assert meta == {}
    assert body == text


def test_strip_frontmatter_parses_scalar_values() -> None:
    """スカラー値の YAML フロントマターを文字列メタデータとして読む。"""
    text = """---
source: sample.txt
date: 2026-05-16
---

# 要約
"""

    meta, body = strip_frontmatter(text)

    assert meta["source"] == "sample.txt"
    assert meta["date"] == "2026-05-16"
    assert body == "# 要約\n"


def test_strip_frontmatter_parses_yaml_list_keywords() -> None:
    """keywords が YAML リスト形式でも空文字にせず検索用文字列へ変換する。"""
    text = """---
source: sample.txt
keywords:
- ECU
- 設計
- CAN通信
---

本文
"""

    meta, body = strip_frontmatter(text)

    assert meta["keywords"] == "ECU, 設計, CAN通信"
    assert body == "本文\n"


def test_strip_frontmatter_handles_inline_yaml_list_keywords() -> None:
    """keywords がインライン YAML リスト形式でも同じ文字列へ変換する。"""
    text = """---
keywords: [ECU, 設計]
---

本文
"""

    meta, _ = strip_frontmatter(text)

    assert meta["keywords"] == "ECU, 設計"
