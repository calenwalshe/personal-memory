"""
intake_doc.py — Document intake adapter for L0 universal source layer.

Reads a markdown or text file, creates a source record in sources.db,
and splits it into segments (by ## headings or paragraph blocks).

Usage:
  from intake_doc import ingest_document
  result = ingest_document("/path/to/file.md", project="personal-memory")

CLI:
  python3 intake_doc.py <file_path> [--project <name>] [--title <title>]
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from source_store import create_source, create_segments_batch


# Minimum segment length to keep (chars) — skip tiny fragments
MIN_SEGMENT_CHARS = 30


def _split_by_headings(text: str) -> list[dict]:
    """Split markdown text by ## headings. Falls back to paragraph splitting."""
    # Try heading-based splitting first
    heading_pattern = re.compile(r'^(#{1,3})\s+(.+)$', re.MULTILINE)
    matches = list(heading_pattern.finditer(text))

    if len(matches) >= 3:
        # Enough headings to use heading-based segments
        segments = []
        for i, match in enumerate(matches):
            start = match.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            content = text[start:end].strip()
            if len(content) >= MIN_SEGMENT_CHARS:
                segments.append({
                    "segment_type": "section",
                    "ordinal": len(segments),
                    "content": content,
                    "char_start": start,
                    "char_end": end,
                    "metadata": {"heading": match.group(2).strip()},
                })

        # Capture any content before the first heading
        if matches and matches[0].start() > MIN_SEGMENT_CHARS:
            preamble = text[:matches[0].start()].strip()
            if len(preamble) >= MIN_SEGMENT_CHARS:
                segments.insert(0, {
                    "segment_type": "section",
                    "ordinal": 0,
                    "content": preamble,
                    "char_start": 0,
                    "char_end": matches[0].start(),
                    "metadata": {"heading": "(preamble)"},
                })
                # Renumber ordinals
                for i, seg in enumerate(segments):
                    seg["ordinal"] = i

        return segments

    # Fall back to paragraph-based splitting
    return _split_by_paragraphs(text)


def _split_by_paragraphs(text: str) -> list[dict]:
    """Split text by double newlines into paragraph segments."""
    blocks = re.split(r'\n{2,}', text)
    segments = []
    pos = 0

    for block in blocks:
        block_stripped = block.strip()
        if len(block_stripped) < MIN_SEGMENT_CHARS:
            pos = text.find(block, pos) + len(block)
            continue

        char_start = text.find(block, pos)
        char_end = char_start + len(block)

        segments.append({
            "segment_type": "paragraph",
            "ordinal": len(segments),
            "content": block_stripped,
            "char_start": char_start,
            "char_end": char_end,
        })
        pos = char_end

    return segments


def ingest_document(
    file_path: str,
    project: str = None,
    title: str = None,
    source_time: str = None,
) -> dict:
    """Ingest a document file into sources.db.

    Returns dict with source_id, segment_count, char_count.
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    text = path.read_text(encoding="utf-8")

    # Default title from filename
    if not title:
        title = path.stem.replace("-", " ").replace("_", " ")

    # Create source record
    source_id = create_source(
        source_type="doc",
        title=title,
        raw_content=text,
        file_path=str(path.resolve()),
        project=project,
        source_time=source_time,
        capture_quality="complete",
        metadata={"file_ext": path.suffix, "char_count": len(text)},
    )

    # Split into segments
    segments = _split_by_headings(text)

    # Create segments in batch
    seg_ids = []
    if segments:
        seg_ids = create_segments_batch(source_id, segments)

    return {
        "source_id": source_id,
        "segment_count": len(seg_ids),
        "char_count": len(text),
        "title": title,
    }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Ingest a document into the vault")
    parser.add_argument("file", help="Path to markdown/text file")
    parser.add_argument("--project", help="Project scope")
    parser.add_argument("--title", help="Document title (default: filename)")
    args = parser.parse_args()

    result = ingest_document(args.file, project=args.project, title=args.title)
    print(f"Ingested: {result['title']}")
    print(f"  Source ID:  {result['source_id']}")
    print(f"  Segments:   {result['segment_count']}")
    print(f"  Chars:      {result['char_count']:,}")
