"""ingest_db.py
Step 3: data/notes/*.md を読み込み、ChromaDB に埋め込みベクターとして登録するスクリプト。

使い方:
    # 単一ファイル投入
    python scripts/ingest_db.py data/notes/meeting_001_2026-05-13_1030.md

    # data/notes/ 配下の .md を全件投入
    python scripts/ingest_db.py --all

設計メモ:
    - 1 ファイルを「見出し or 文字数」でチャンク分割
    - チャンクごとに nomic-embed-text で埋め込みを生成
    - ChromaDB（永続化ディレクトリ: data/chromadb/）に upsert
    - ドキュメント ID は "<filename>__chunk<index>" 形式（同名ファイル再投入時は上書きされる）
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import chromadb
import ollama
import yaml

from config_loader import load_settings, resolve_path


def _stringify_frontmatter_value(value: Any) -> str:
    """ChromaDB metadata に入れられるようフロントマター値を文字列化する。

    YAML の `keywords` は summarize.py がリスト形式で出力するため、
    そのまま ChromaDB metadata に渡すのではなく検索しやすい文字列に正規化する。
    """
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(_stringify_frontmatter_value(item) for item in value if item is not None)
    if isinstance(value, tuple):
        return ", ".join(_stringify_frontmatter_value(item) for item in value if item is not None)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def strip_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Markdown 先頭の YAML フロントマターを分離する。

    Args:
        text: Markdown 全文。

    Returns:
        (フロントマターを簡易パースした辞書, 本文)。
    """
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    meta_block = parts[1].strip()
    body = parts[2].lstrip("\n")
    try:
        parsed = yaml.safe_load(meta_block) or {}
    except yaml.YAMLError:
        return {}, body
    if not isinstance(parsed, dict):
        return {}, body

    meta: dict[str, str] = {
        str(key).strip(): _stringify_frontmatter_value(value).strip()
        for key, value in parsed.items()
        if str(key).strip()
    }
    return meta, body


def chunk_text(text: str, max_chars: int, overlap: int) -> list[str]:
    """テキストを文/行の境界を考慮してチャンク分割する。

    まず Markdown 見出し（## など）で大きく分け、それでも長いセクションは
    句点「。」や改行を境界として文/行の単位に区切り、max_chars を超えない
    範囲で貪欲に連結してチャンクを作る。これにより日本語文の途中でぶつ切りに
    なる問題を防ぎ、検索精度を向上させる。

    Args:
        text: 入力テキスト。
        max_chars: 1 チャンクあたりの最大文字数。
        overlap: 前チャンクと重ねる文字数（文脈ロス防止）。

    Returns:
        チャンクのリスト（空チャンクは除外）。
    """

    def split_into_sentences(section: str) -> list[str]:
        """セクションを「文」の単位に分割する。

        句点「。」の直後、または改行を区切りとして文を切り出す。
        区切り文字自体は直前の文に含める（句点は文末として保持）。
        """
        # 句点「。」の直後 か 改行 を区切りに分割する正規表現。
        # split すると区切り文字が消えるため、findall + 残余を使って句点を保持する。
        parts: list[str] = []
        # 「句点で終わる塊」または「改行を含まない行末の塊」を順に取り出す
        pattern = re.compile(r"[^。\n]*。|[^\n]+")
        for m in pattern.finditer(section):
            s = m.group()
            if s:
                parts.append(s)
        return parts if parts else [section]

    def split_long_section(section: str) -> list[str]:
        """max_chars を超えるセクションを文境界ベースでチャンク化する。

        手順:
          1. セクションを文/行の単位に分割する。
          2. 各文を max_chars を超えない範囲で貪欲に結合してチャンクを作る。
          3. 1 文が単体で max_chars を超える場合は文字数で強制分割（フォールバック）。
          4. 各チャンクの先頭に前チャンク末尾の overlap 文字分を付加する。
        """
        sentences = split_into_sentences(section)
        result: list[str] = []
        # 現在構築中のチャンクに含まれる文のリスト
        current_parts: list[str] = []
        current_len = 0

        for sent in sentences:
            sent = sent.strip()
            if not sent:
                continue

            # 1 文自体が max_chars を超える極端ケース → 文字数で強制分割（フォールバック）
            if len(sent) > max_chars:
                # まず積み上げ中の文があればチャンクとして確定
                if current_parts:
                    result.append("".join(current_parts))
                    current_parts = []
                    current_len = 0
                # overlap 付与後も max_chars に収まるよう、スライスサイズを調整する。
                # 後段の overlap 処理で先頭に最大 overlap 文字が追加されるため、
                # 1 スライス = max_chars - overlap 文字として切る（2枚目以降の長さを担保）。
                # ただし overlap が 0 の場合は max_chars そのまま使う。
                slice_size = max(1, max_chars - overlap)
                start = 0
                while start < len(sent):
                    result.append(sent[start : start + slice_size])
                    start += slice_size
                continue

            # 今の文を追加するとチャンクが max_chars を超えるか判定
            if current_len + len(sent) > max_chars and current_parts:
                # 現在の積み上げを 1 チャンクとして確定
                result.append("".join(current_parts))
                current_parts = []
                current_len = 0

            current_parts.append(sent)
            current_len += len(sent)

        # ループ終了後に残った文もチャンクとして確定
        if current_parts:
            result.append("".join(current_parts))

        # --- overlap 処理: 前チャンクの末尾 overlap 文字を次チャンクの先頭に付加 ---
        # 文脈の連続性を保つため、各チャンクの先頭に前チャンク末尾を重ねる。
        if overlap <= 0 or len(result) <= 1:
            return result

        overlapped: list[str] = [result[0]]
        for i in range(1, len(result)):
            prefix = result[i - 1][-overlap:]  # 前チャンク末尾 overlap 文字
            overlapped.append(prefix + result[i])
        return overlapped

    # --- メイン処理: まず Markdown 見出しでセクションに分割 ---
    # （先頭の `---` フロントマターは事前に strip_frontmatter で除去済みが前提）
    sections = re.split(r"\n(?=#{1,6}\s)", text)
    chunks: list[str] = []

    for section in sections:
        section = section.strip()
        if not section:
            continue

        if len(section) <= max_chars:
            # max_chars 以内なら分割不要、そのまま 1 チャンクとして追加
            chunks.append(section)
        else:
            # 長いセクションは文/行境界ベースで分割
            chunks.extend(split_long_section(section))

    # 空チャンクを除外して返す
    return [c for c in chunks if c.strip()]


def embed_text(text: str, embedding_cfg: dict[str, Any]) -> list[float]:
    """Ollama で埋め込みベクターを生成する。

    Args:
        text: 入力テキスト。
        embedding_cfg: settings.yaml の embedding セクション。

    Returns:
        埋め込みベクター（float のリスト）。
    """
    client = ollama.Client(host=embedding_cfg["host"])
    response = client.embeddings(model=embedding_cfg["model"], prompt=text)
    return list(response["embedding"])


def get_or_create_collection(
    chromadb_cfg: dict[str, Any], persist_dir: Path
) -> chromadb.Collection:
    """ChromaDB のコレクションを取得（無ければ作成）する。"""
    client = chromadb.PersistentClient(path=str(persist_dir))
    return client.get_or_create_collection(
        name=chromadb_cfg["collection_name"],
        metadata={"hnsw:space": "cosine"},
    )


def ingest_file(
    md_path: Path,
    collection: chromadb.Collection,
    chromadb_cfg: dict[str, Any],
    embedding_cfg: dict[str, Any],
) -> int:
    """単一の Markdown ファイルを ChromaDB に投入する。

    投入順序（delete-then-add トランザクション）:
        1. 同じ source_file メタを持つ既存チャンクを delete で全削除
        2. 新しいチャンクを add で投入

    これにより以下を防ぐ:
        - 再処理時のチャンク数変動による孤児チャンク
          （例: 前回 5 チャンク → 今回 3 チャンクの場合、upsert だと
                chunk003/004 が古いまま残る）
        - pipeline.py が Step 3 の途中で停止した場合の中途半端な投入残骸

    中断時のトレードオフ:
        delete 完了後 / add 完了前に死ぬと、ChromaDB から該当ノートが一時的に消える。
        ただし「重複」より「欠落」の方が検索結果としてマシ（次回再投入で復元される）。

    Args:
        md_path: 投入する .md のパス。
        collection: ChromaDB コレクション。
        chromadb_cfg: settings.yaml の chromadb セクション。
        embedding_cfg: settings.yaml の embedding セクション。

    Returns:
        登録したチャンク数。
    """
    text = md_path.read_text(encoding="utf-8")
    meta, body = strip_frontmatter(text)

    chunks = chunk_text(
        body,
        max_chars=chromadb_cfg["chunk_chars"],
        overlap=chromadb_cfg["chunk_overlap_chars"],
    )
    if not chunks:
        print(f"[warn] チャンクが生成されませんでした: {md_path.name}")
        return 0

    ids: list[str] = []
    documents: list[str] = []
    embeddings: list[list[float]] = []
    metadatas: list[dict[str, Any]] = []

    for i, chunk in enumerate(chunks):
        chunk_id = f"{md_path.stem}__chunk{i:03d}"
        embedding = embed_text(chunk, embedding_cfg)
        ids.append(chunk_id)
        documents.append(chunk)
        embeddings.append(embedding)
        metadatas.append(
            {
                "source_file": md_path.name,
                "source_path": str(md_path),
                "chunk_index": i,
                # フロントマターのメタを継承（None は ChromaDB が嫌うので空文字に）
                "date": meta.get("date", ""),
                "keywords": meta.get("keywords", ""),
            }
        )

    # --- Step 1: 同 source_file の既存チャンクを削除 ---
    # 失敗（コレクションが空 / 該当なし）でも例外を投げない実装にする。
    # where 条件で source_file が一致するエントリを全削除。
    try:
        collection.delete(where={"source_file": md_path.name})
    except Exception as e:
        # 削除失敗は警告のみ（既存が無い場合もここに入る可能性がある）
        print(f"[warn] 既存チャンク削除でエラー（続行します）: {e}", file=sys.stderr)

    # --- Step 2: 新規チャンクを追加 ---
    # delete-then-add 戦略のため upsert ではなく add を使う。
    # 同 ID が残っていた場合は add が例外を投げるが、Step 1 で削除済みのため
    # 通常はぶつからない。
    collection.add(
        ids=ids,
        documents=documents,
        embeddings=embeddings,
        metadatas=metadatas,
    )
    print(f"[ingest] {md_path.name}: {len(chunks)} チャンク登録")
    return len(chunks)


def main() -> int:
    """エントリポイント。"""
    parser = argparse.ArgumentParser(description="Markdown ノートを ChromaDB に投入")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("note", nargs="?", type=str, help="単一ファイル投入")
    group.add_argument(
        "--all", action="store_true", help="data/notes/ 配下の .md を全件投入"
    )
    args = parser.parse_args()

    settings = load_settings()
    chromadb_cfg = settings["chromadb"]
    embedding_cfg = settings["embedding"]
    notes_dir = resolve_path(settings["paths"]["notes_dir"])
    chromadb_dir = resolve_path(settings["paths"]["chromadb_dir"])
    chromadb_dir.mkdir(parents=True, exist_ok=True)

    collection = get_or_create_collection(chromadb_cfg, chromadb_dir)
    print(f"[chromadb] collection='{collection.name}' を使用")

    # 投入対象を決定
    targets: list[Path]
    if args.all:
        targets = sorted(notes_dir.glob("*.md"))
        if not targets:
            print(f"[error] {notes_dir} に .md ファイルがありません。", file=sys.stderr)
            return 1
    else:
        targets = [Path(args.note).resolve()]
        if not targets[0].exists():
            print(f"[error] ファイルが見つかりません: {targets[0]}", file=sys.stderr)
            return 1

    total_chunks = 0
    for md_path in targets:
        try:
            total_chunks += ingest_file(
                md_path, collection, chromadb_cfg, embedding_cfg
            )
        except Exception as e:
            # 1 ファイル失敗で全体停止させない
            print(f"[error] {md_path.name} の投入に失敗: {e}", file=sys.stderr)

    print(
        f"\n[done] 合計 {total_chunks} チャンクを登録しました。"
        f"  コレクション総件数: {collection.count()}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
