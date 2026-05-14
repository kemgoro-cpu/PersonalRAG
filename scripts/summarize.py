"""summarize.py
Step 2: 文字起こしテキスト（.txt）を Ollama 経由で Gemma に投げ、
要約・ToDo・キーワード等を抽出して Markdown ファイル（.md）に保存するスクリプト。

使い方:
    python scripts/summarize.py <文字起こしテキストのパス>

例:
    python scripts/summarize.py data/transcripts/meeting_001_2026-05-13_1030.txt

LLM 出力:
    config/prompts/summarize.txt に従って LLM は JSON を返す想定。
    JSON パース失敗時は本文をそのまま埋め込む（落とさない）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import ollama
import yaml

from config_loader import PROJECT_ROOT, load_settings, resolve_path


def load_prompt_template() -> str:
    """要約用プロンプトテンプレートを読み込む。"""
    path = PROJECT_ROOT / "config" / "prompts" / "summarize.txt"
    return path.read_text(encoding="utf-8")


def load_recording_meta(transcript_path: Path) -> dict[str, str] | None:
    """transcript と同じディレクトリにある .meta.json を読み込む。

    フェーズ B で record_gui.py が録音時に生成したメタ情報を取得し、
    Markdown ノートのフロントマターに反映するために使う。

    meta.json のパスは pipeline.py が `{transcript_stem}.meta.json` として
    transcript 隣に配置しているため、同名ファイルを探すだけでよい。

    Args:
        transcript_path: 文字起こしファイルのパス。

    Returns:
        メタ情報辞書。ファイルが存在しない・読み込み失敗の場合は None を返す。
        キー: "title", "participants", "topic", "recorded_at"
    """
    meta_path = transcript_path.parent / (transcript_path.stem + ".meta.json")
    if not meta_path.exists():
        return None
    try:
        text = meta_path.read_text(encoding="utf-8")
        data = json.loads(text)
        # 各フィールドが欠損していても空文字列でフォールバックする
        return {
            "title": str(data.get("title", "")),
            "participants": str(data.get("participants", "")),
            "topic": str(data.get("topic", "")),
            "recorded_at": str(data.get("recorded_at", "")),
        }
    except Exception as exc:
        print(f"[warn] meta.json の読み込みに失敗（フロントマターなしで続行）: {exc}", file=sys.stderr)
        return None


def build_recording_frontmatter(meta: dict[str, str]) -> str:
    """録音メタ情報を YAML フロントマター文字列に変換する。

    PyYAML の safe_dump を使うことで特殊文字を含むタイトルも安全に出力できる。
    例: タイトルに : や " が含まれる場合も自動的に引用符で囲まれる。

    Args:
        meta: load_recording_meta() が返した辞書。

    Returns:
        "---\\n...\\n---\\n" 形式の文字列。
        全フィールドが空の場合は空文字列を返す（フロントマターを挿入しない）。
    """
    # 全フィールドが空の場合はフロントマターを出力しない
    if not any(meta.values()):
        return ""
    # yaml.safe_dump で YAML として valid な出力を生成する
    # allow_unicode=True: 日本語をエスケープせずそのまま出力
    # default_flow_style=False: ブロック形式（読みやすい）で出力
    # sort_keys=False: dict の定義順を維持する
    yaml_body = yaml.safe_dump(meta, allow_unicode=True, default_flow_style=False, sort_keys=False)
    return f"---\n{yaml_body}---\n"


def merge_frontmatter(
    recording_meta: dict[str, str] | None,
    base_meta: dict[str, Any],
) -> dict[str, Any]:
    """録音メタと既存メタを1つの dict にマージする。

    キーが衝突した場合は base_meta（生成時メタ）の値を優先する。
    録音メタの recorded_at と既存メタの date は別キーなので衝突しない。

    優先順位の根拠:
        - title/participants/topic/recorded_at は録音前にユーザーが入力した値
        - source/date/generated_at/keywords は LLM 処理で生成した値
        - どちらが「正」かは用途次第だが、生成時に確定する date/keywords は
          上書きしたくないため base_meta を優先とする

    Args:
        recording_meta: load_recording_meta() の戻り値。None なら空辞書として扱う。
        base_meta: source/date/generated_at/keywords など既存メタを格納した辞書。

    Returns:
        マージ後の辞書。キー順序: 録音メタ → 既存メタ の順（sort_keys=False で維持）。
    """
    merged: dict[str, Any] = {}
    # 録音メタを先に入れる（後から上書きされる可能性あり）
    if recording_meta is not None:
        for k, v in recording_meta.items():
            if v:  # 空文字列キーは混入させない
                merged[k] = v
    # 既存メタで上書き（base_meta のキーが優先）
    merged.update(base_meta)
    return merged


def call_ollama(
    prompt: str, llm_cfg: dict[str, Any]
) -> str:
    """Ollama API を叩いて応答テキストを取得する。

    Args:
        prompt: LLM に渡すプロンプト全体。
        llm_cfg: settings.yaml の llm セクション。

    Returns:
        LLM の応答テキスト（生の文字列）。
    """
    client = ollama.Client(host=llm_cfg["host"])
    print(f"[ollama] モデル={llm_cfg['model']} に問い合わせ中（応答待ち）...")
    response = client.generate(
        model=llm_cfg["model"],
        prompt=prompt,
        options={
            "temperature": llm_cfg.get("temperature", 0.3),
            "num_ctx": llm_cfg.get("num_ctx", 8192),
        },
        keep_alive=llm_cfg.get("keep_alive", 0),
    )
    return response.get("response", "")


def extract_json(text: str) -> dict[str, Any] | None:
    """LLM 応答から JSON 部分を抜き出してパースする。

    LLM が前後に余計な文を付けることがあるため、最初の `{` から最後の `}` までを
    抜き出して試行する。

    Args:
        text: LLM の応答文字列。

    Returns:
        パースに成功した辞書、失敗時は None。
    """
    # コードフェンスを取り除く
    cleaned = re.sub(r"```(?:json)?\s*", "", text)
    cleaned = cleaned.replace("```", "")

    # 最初の { から最後の } までを抽出
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    candidate = cleaned[start : end + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as e:
        print(f"[warn] JSON パース失敗: {e}", file=sys.stderr)
        return None


def render_markdown(
    parsed: dict[str, Any] | None,
    raw_response: str,
    source_transcript: Path,
    recording_meta: dict[str, str] | None = None,
) -> str:
    """LLM 応答を Markdown 形式に整形する。

    JSON パースに成功した場合は構造化された Markdown を生成。
    失敗した場合は生応答をそのまま本文に貼り付ける（情報を失わない）。

    フェーズ B: recording_meta が渡された場合は、録音メタ情報
    （title/participants/topic/recorded_at）と既存メタ（source/date/generated_at/keywords）を
    1 つの YAML ブロックに統合する。2 ブロックに分けると ingest_db.py の
    strip_frontmatter が最初の 1 ブロックしか剥がさないため、2 つ目が本文扱いになる回帰を防ぐ。
    全録音メタフィールドが空の場合は既存メタのみのフロントマターを出力する（既存動作を維持）。

    Args:
        parsed: パース済み辞書。None なら failure 時。
        raw_response: LLM の生応答。
        source_transcript: 元の文字起こしファイルパス。
        recording_meta: 録音前に入力したメタ情報。None なら挿入しない。

    Returns:
        Markdown 文字列。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if parsed is None:
        # フォールバック: 生応答をそのまま貼り付け
        # base_meta を組み立てて録音メタとマージしてから1ブロックで出力する
        base_meta: dict[str, Any] = {
            "source": source_transcript.name,
            "date": today,
            "generated_at": now,
            "parse_status": "failed",
        }
        merged_meta = merge_frontmatter(recording_meta, base_meta)
        yaml_body = yaml.safe_dump(
            merged_meta, allow_unicode=True, default_flow_style=False, sort_keys=False
        )
        unified_frontmatter = f"---\n{yaml_body}---\n"
        return (
            unified_frontmatter
            + "\n"
            + "# 要約（JSON パース失敗、生応答）\n\n"
            + f"```\n{raw_response.strip()}\n```\n"
        )

    summary = parsed.get("summary", "")
    topics = parsed.get("topics", []) or []
    decisions = parsed.get("decisions", []) or []
    todos = parsed.get("todos", []) or []
    questions = parsed.get("questions", []) or []
    keywords = parsed.get("keywords", []) or []

    # フロントマター用の既存メタ辞書
    # keywords は YAML リスト形式で出力したいため list のまま渡す
    # （yaml.safe_dump が自動的にブロックリストに変換する）
    existing_meta: dict[str, Any] = {
        "source": source_transcript.name,
        "date": today,
        "generated_at": now,
        "keywords": keywords,
    }

    # 録音メタと既存メタを1つの辞書にマージしてから YAML 化する
    # → フロントマターは必ず1ブロックになる（ingest_db.py の strip_frontmatter と整合）
    merged = merge_frontmatter(recording_meta, existing_meta)
    yaml_body = yaml.safe_dump(
        merged, allow_unicode=True, default_flow_style=False, sort_keys=False
    )
    unified_frontmatter = f"---\n{yaml_body}---\n\n"

    body: list[str] = []
    body.append(f"# 要約 ({today})\n")
    body.append(f"**ソース**: `{source_transcript.name}`\n")

    body.append("## サマリー\n")
    body.append(summary if summary else "_（要約が空です）_")
    body.append("")

    if topics:
        body.append("## 主要トピック\n")
        for t in topics:
            body.append(f"- {t}")
        body.append("")

    if decisions:
        body.append("## 決定事項\n")
        for d in decisions:
            body.append(f"- {d}")
        body.append("")

    if todos:
        body.append("## ToDo\n")
        body.append("| 内容 | 担当 | 期限 |")
        body.append("|---|---|---|")
        for t in todos:
            task = t.get("task", "")
            assignee = t.get("assignee", "") or "—"
            due = t.get("due", "") or "—"
            body.append(f"| {task} | {assignee} | {due} |")
        body.append("")

    if questions:
        body.append("## 未解決の論点\n")
        for q in questions:
            body.append(f"- {q}")
        body.append("")

    if keywords:
        body.append("## キーワード\n")
        body.append(" / ".join(f"`{k}`" for k in keywords))
        body.append("")

    return unified_frontmatter + "\n".join(body) + "\n"


def build_output_path(transcript_path: Path, notes_dir: Path) -> Path:
    """出力 Markdown のパスを決定する。

    例: meeting_001_2026-05-13_1030.txt → meeting_001_2026-05-13_1030.md
    """
    return notes_dir / f"{transcript_path.stem}.md"


def main() -> int:
    """エントリポイント。"""
    parser = argparse.ArgumentParser(
        description="文字起こしテキストを要約して Markdown 保存する"
    )
    parser.add_argument(
        "transcript", type=str, help="入力テキスト（文字起こし結果）のパス"
    )
    args = parser.parse_args()

    transcript_path = Path(args.transcript).resolve()
    if not transcript_path.exists():
        print(f"[error] ファイルが見つかりません: {transcript_path}", file=sys.stderr)
        return 1

    settings = load_settings()
    llm_cfg = settings["llm"]
    notes_dir = resolve_path(settings["paths"]["notes_dir"])
    notes_dir.mkdir(parents=True, exist_ok=True)

    transcript_text = transcript_path.read_text(encoding="utf-8")
    if not transcript_text.strip():
        print("[error] 文字起こしテキストが空です。", file=sys.stderr)
        return 2

    # プロンプト組み立て
    template = load_prompt_template()
    prompt = template.replace("{TRANSCRIPT}", transcript_text)

    # LLM 呼び出し
    try:
        raw_response = call_ollama(prompt, llm_cfg)
    except Exception as e:
        print(
            f"[error] Ollama 呼び出しに失敗: {e}\n"
            f"        Ollama サーバが起動しているか、モデル '{llm_cfg['model']}' が pull 済みか確認してください。",
            file=sys.stderr,
        )
        return 3

    if not raw_response.strip():
        print("[error] LLM から空応答が返りました。", file=sys.stderr)
        return 4

    # 録音前に入力したメタ情報を読み込む（フェーズ B）
    # transcript と同名の .meta.json が存在すれば Markdown 先頭にフロントマターを挿入する
    recording_meta = load_recording_meta(transcript_path)
    if recording_meta:
        print(f"[meta] 録音メタ情報を読み込みました: title={recording_meta.get('title', '')}")
    else:
        print("[meta] meta.json が見つからないため、フロントマターなしで処理します")

    # JSON 抽出 → Markdown 化
    parsed = extract_json(raw_response)
    markdown = render_markdown(parsed, raw_response, transcript_path, recording_meta=recording_meta)

    output_path = build_output_path(transcript_path, notes_dir)
    output_path.write_text(markdown, encoding="utf-8")
    print(f"[output] 保存しました: {output_path}")
    if parsed is None:
        print(
            "[warn] JSON パースに失敗したため、生応答を保存しました。\n"
            "       プロンプトの調整やモデル変更を検討してください。"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
