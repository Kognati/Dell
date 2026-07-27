#!/usr/bin/env python3
"""Extract text from a folder of PDFs into chunks.jsonl plus full-text sidecars.

Mechanical only: parse bytes, slice spans, write files. No classification.

Offset contract -- everything downstream depends on it:

  * A document's full text is the concatenation of its page texts, in order,
    each page terminated by exactly one "\\n".
  * Every chunk's `text` is exactly full_text[char_start:char_end].
  * The sidecar `<doc_id>.txt` is that same full text, written with newline
    translation disabled, so validate.py can re-slice it independently.
  * `<doc_id>.pages.json` records each page's [start, end) span so validate.py
    can also verify the cited page number, not just the characters.
"""

from __future__ import annotations

import argparse
import re
import sys
from bisect import bisect_left, bisect_right
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

from schemas import (  # noqa: E402
    Chunk,
    die,
    eprint,
    page_for_offset,
    pagemap_path,
    require,
    safe_doc_id,
    stable_id,
    write_fulltext,
    write_json,
    write_jsonl,
)

DEFAULT_TARGET_CHARS = 1500
DEFAULT_OVERLAP_CHARS = 200

# A paragraph break is a blank line, plus any whitespace that follows it.
PARAGRAPH_BREAK_RE = re.compile(r"\n[ \t]*\n\s*")

# Lines that are page furniture rather than a title.
_FURNITURE_RE = re.compile(
    r"^(?:page\s*\d+|\d+|[ivxlcdm]+|exhibit\s+[a-z0-9]+|confidential.*)$",
    re.IGNORECASE,
)


# --------------------------------------------------------------------------
# PDF text extraction
# --------------------------------------------------------------------------


def extract_document(path: Path) -> Tuple[str, List[List[int]], str, str]:
    """Return (full_text, page_spans, first_page_text, metadata_title)."""
    fitz = require("fitz", "pymupdf", "PDF text extraction")
    try:
        doc = fitz.open(path)
    except Exception as exc:  # pragma: no cover - depends on input files
        raise RuntimeError(f"cannot open PDF: {exc}") from exc

    parts: List[str] = []
    page_spans: List[List[int]] = []
    first_page_text = ""
    position = 0
    try:
        metadata_title = (doc.metadata or {}).get("title") or ""
        for index, page in enumerate(doc):
            text = page.get_text("text") or ""
            # Normalize line endings only. Any other mutation would desync the
            # offsets from the text an agent later reads back.
            text = text.replace("\r\n", "\n").replace("\r", "\n")
            if not text.endswith("\n"):
                text += "\n"
            if index == 0:
                first_page_text = text
            parts.append(text)
            page_spans.append([position, position + len(text)])
            position += len(text)
    finally:
        doc.close()

    return "".join(parts), page_spans, first_page_text, metadata_title


def guess_title(first_page_text: str, metadata_title: str, doc_id: str) -> str:
    """Best-effort document title from the first page, then metadata, then the
    filename stem. Cosmetic only -- nothing downstream depends on it."""
    for line in first_page_text.splitlines()[:15]:
        candidate = " ".join(line.split())
        if len(candidate) < 8 or len(candidate) > 200:
            continue
        if _FURNITURE_RE.match(candidate):
            continue
        letters = sum(char.isalpha() for char in candidate)
        if letters < len(candidate) * 0.5:
            continue
        return candidate
    cleaned = " ".join((metadata_title or "").split())
    if 3 <= len(cleaned) <= 200:
        return cleaned
    return doc_id


# --------------------------------------------------------------------------
# Chunking. Spans only -- a chunk is always a literal slice of full text.
# --------------------------------------------------------------------------


def paragraph_spans(text: str) -> List[Tuple[int, int]]:
    """Split full text into paragraph spans, dropping the separators."""
    spans: List[Tuple[int, int]] = []
    position = 0
    for match in PARAGRAPH_BREAK_RE.finditer(text):
        if match.start() > position:
            spans.append((position, match.start()))
        position = match.end()
    if position < len(text):
        spans.append((position, len(text)))
    return spans


def _largest_in(values: Sequence[int], low: int, high: int) -> Optional[int]:
    """Largest v in `values` (sorted) with low < v <= high, else None."""
    index = bisect_right(values, high) - 1
    if index >= 0 and values[index] > low:
        return values[index]
    return None


def _smallest_in(values: Sequence[int], low: int, high: int) -> Optional[int]:
    """Smallest v in `values` (sorted) with low <= v < high, else None."""
    index = bisect_left(values, low)
    if index < len(values) and values[index] < high:
        return values[index]
    return None


def _snap_back(text: str, start: int, limit: int) -> int:
    """Choose a cut at or before `limit`, preferring a whitespace boundary in
    the back half of the window. Always returns a value in (start, limit]."""
    floor = start + max((limit - start) // 2, 1)
    for needle in ("\n", " "):
        cut = text.rfind(needle, floor, limit)
        if cut > start:
            return cut + 1  # the boundary character stays with the left chunk
    return limit


def _snap_forward(text: str, position: int, limit: int) -> int:
    """Move `position` forward off the middle of a word, staying below `limit`."""
    if position <= 0 or position >= limit:
        return position
    if text[position - 1].isspace():
        return position
    cursor = position
    while cursor < limit and not text[cursor].isspace():
        cursor += 1
    while cursor < limit and text[cursor].isspace():
        cursor += 1
    return cursor if cursor < limit else position


def build_chunk_spans(
    text: str, target: int, overlap: int
) -> List[Tuple[int, int]]:
    """Cut full text into ~`target`-char chunks with ~`overlap` chars of trailing
    context repeated at the head of the next chunk.

    A char cursor drives the walk, and paragraph boundaries are preferred for
    both the end of a chunk and the start of the next one. Falling back to a
    whitespace cut matters more than it looks: PDF extraction often yields no
    blank lines at all, and a paragraph-only scheme would then produce chunks
    with zero overlap and lose any clause straddling a boundary.

    Guarantees, all of which downstream stages depend on:
      * every chunk is exactly text[start:end]
      * starts strictly increase, so the walk terminates and ids are unique
      * chunks are contiguous or overlapping -- never a gap
    """
    total = len(text)
    if total == 0:
        return []
    units = paragraph_spans(text)
    unit_starts = [start for start, _ in units]
    unit_ends = [end for _, end in units]
    # Index just past the last non-whitespace character. Text beyond it is a
    # trailing separator and belongs to the final chunk -- leaving it uncovered
    # makes the cursor creep forward one paragraph at a time without finishing.
    content_end = len(text.rstrip())

    chunks: List[Tuple[int, int]] = []
    position = 0
    while position < total:
        limit = min(position + target, total)
        # The cut must advance past the previous chunk's end, otherwise a
        # paragraph longer than `target` makes the walk stall on the same
        # boundary and coverage stops mid-document.
        floor = position if not chunks else max(position, chunks[-1][1])
        end = _largest_in(unit_ends, floor, limit)
        if end is None:
            end = _snap_back(text, floor, limit)
        if end >= content_end:
            end = total
        if chunks and end <= chunks[-1][1]:
            break  # the remainder is already covered
        chunks.append((position, end))
        if end >= total:
            break

        window_low = max(position + 1, end - overlap)
        following = _smallest_in(unit_starts, window_low, end)
        if following is None:
            following = _snap_forward(text, window_low, end)
        position = max(following, position + 1)
    return chunks


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def collect_pdfs(root: Path) -> List[Path]:
    if root.is_file():
        return [root] if root.suffix.lower() == ".pdf" else []
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() == ".pdf"
    )


def assign_doc_ids(paths: Sequence[Path], root: Path) -> Dict[Path, str]:
    """One id per file. Colliding stems get a short path hash suffix."""
    seen: Dict[str, int] = {}
    result: Dict[Path, str] = {}
    for path in paths:
        try:
            base = safe_doc_id(path.stem)
        except ValueError:
            base = stable_id("doc", str(path))
        seen[base] = seen.get(base, 0) + 1
        if seen[base] > 1:
            try:
                relative = path.relative_to(root)
            except ValueError:
                relative = path
            base = f"{base}_{stable_id('', str(relative))[:6]}"
        result[path] = base
    return result


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Extract PDFs into chunks.jsonl and per-document full-text "
            "sidecars. Offsets are relative to full-document text."
        )
    )
    parser.add_argument(
        "--input", required=True, help="directory of PDFs (searched recursively)"
    )
    parser.add_argument("--output", required=True, help="chunks.jsonl to write")
    parser.add_argument(
        "--fulltext-dir",
        required=True,
        help="directory for <doc_id>.txt and <doc_id>.pages.json sidecars",
    )
    parser.add_argument(
        "--target-chars",
        type=int,
        default=DEFAULT_TARGET_CHARS,
        help=f"approximate chunk size (default {DEFAULT_TARGET_CHARS})",
    )
    parser.add_argument(
        "--overlap-chars",
        type=int,
        default=DEFAULT_OVERLAP_CHARS,
        help=f"trailing context repeated per chunk (default {DEFAULT_OVERLAP_CHARS})",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    root = Path(args.input).expanduser()
    if not root.exists():
        die(f"--input path does not exist: {root}")
    if args.target_chars < 200:
        die("--target-chars must be at least 200")
    if not 0 <= args.overlap_chars < args.target_chars:
        die("--overlap-chars must be >= 0 and less than --target-chars")

    pdfs = collect_pdfs(root)
    if not pdfs:
        die(f"no PDFs found under {root} (this pipeline handles PDF only)")

    fulltext_dir = Path(args.fulltext_dir).expanduser()
    fulltext_dir.mkdir(parents=True, exist_ok=True)
    doc_ids = assign_doc_ids(pdfs, root)

    chunks: List[Chunk] = []
    failures = 0
    for path in pdfs:
        doc_id = doc_ids[path]
        try:
            full_text, page_spans, first_page, metadata_title = extract_document(path)
        except RuntimeError as exc:
            eprint(f"[ingest] SKIP {path.name}: {exc}")
            failures += 1
            continue

        if not full_text.strip():
            eprint(
                f"[ingest] SKIP {path.name}: no extractable text "
                "(scanned PDFs are out of scope -- no OCR)"
            )
            failures += 1
            continue

        doc_title = guess_title(first_page, metadata_title, doc_id)
        write_fulltext(fulltext_dir, doc_id, full_text)
        write_json(
            pagemap_path(fulltext_dir, doc_id),
            {
                "doc_id": doc_id,
                "doc_title": doc_title,
                "source_filename": path.name,
                "char_length": len(full_text),
                "pages": page_spans,
            },
        )

        spans = build_chunk_spans(full_text, args.target_chars, args.overlap_chars)
        for char_start, char_end in spans:
            text = full_text[char_start:char_end]
            if not text.strip():
                continue
            chunks.append(
                Chunk(
                    chunk_id=stable_id("chunk", doc_id, char_start, char_end),
                    doc_id=doc_id,
                    doc_title=doc_title,
                    page=page_for_offset(page_spans, char_start),
                    char_start=char_start,
                    char_end=char_end,
                    text=text,
                )
            )
        eprint(
            f"[ingest] {path.name} -> doc_id={doc_id} pages={len(page_spans)} "
            f"chars={len(full_text)} chunks={len(spans)}"
        )

    if not chunks:
        die("no text extracted from any PDF; nothing to write")

    written = write_jsonl(Path(args.output), chunks)
    eprint(
        f"[ingest] wrote {written} chunks from {len(pdfs) - failures}/{len(pdfs)} "
        f"documents to {args.output}; full text in {fulltext_dir}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
