#!/usr/bin/env python3
"""Score findings against CUAD annotations.

CUAD ships its own text extraction, so its character offsets are not comparable
to ours. Rather than trust two extractions to agree, this script works entirely
in CUAD's coordinate space: gold spans are already there, and each finding's
verified quote is located inside the CUAD context by whitespace-tolerant search.
Both spans then live on the same axis and IoU is meaningful.

A match requires: same document, same mapped clause category, and span
IoU >= 0.5 (configurable). Greedy one-to-one assignment by descending IoU.

Quotes that cannot be located in the CUAD context are reported separately as
`unlocatable_findings` and counted as false positives, because an unverifiable
match is not a match. Gold spans whose text cannot be found are reported as
`unlocatable_gold` and still counted as misses -- dropping them would inflate
recall.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

from schemas import (  # noqa: E402
    CLAUSE_TYPES,
    FULLTEXT_SUFFIX,
    die,
    eprint,
    read_jsonl,
    write_json,
)

DEFAULT_IOU = 0.5

# CUAD category label -> our clause_type. Categories absent here are ignored on
# both sides, so an unmapped CUAD annotation never counts as a miss.
CUAD_CATEGORY_MAP: Dict[str, str] = {
    "change of control": "change_of_control",
    "anti-assignment": "anti_assignment",
    "most favored nation": "mfn",
    "cap on liability": "indemnity_cap",
    "uncapped liability": "indemnity_cap",
    "renewal term": "auto_renewal",
    "notice period to terminate renewal": "auto_renewal",
    "termination for convenience": "termination_convenience",
    "exclusivity": "exclusivity",
    "governing law": "governing_law",
}

_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")


def normalize_doc_key(name: str) -> str:
    stem = re.sub(r"\.(pdf|txt|json)$", "", str(name), flags=re.IGNORECASE)
    return _NORMALIZE_RE.sub("", stem.lower())


def category_from_question(question: str) -> Optional[str]:
    """CUAD questions embed the category in double quotes."""
    match = re.search(r'"([^"]+)"', question or "")
    label = (match.group(1) if match else question or "").strip().lower()
    return CUAD_CATEGORY_MAP.get(label)


def locate(text: str, needle: str) -> Optional[Tuple[int, int]]:
    """Whitespace-tolerant substring search. Same rule as validate.py."""
    if not needle.strip():
        return None
    index = text.find(needle)
    if index >= 0:
        return index, index + len(needle)
    tokens = needle.split()
    if not tokens:
        return None
    try:
        match = re.compile(r"\s+".join(re.escape(t) for t in tokens)).search(text)
    except re.error:  # pragma: no cover
        return None
    return (match.start(), match.end()) if match else None


def iou(a: Tuple[int, int], b: Tuple[int, int]) -> float:
    overlap = max(0, min(a[1], b[1]) - max(a[0], b[0]))
    if overlap == 0:
        return 0.0
    union = (a[1] - a[0]) + (b[1] - b[0]) - overlap
    return overlap / union if union > 0 else 0.0


# --------------------------------------------------------------------------
# CUAD loading
# --------------------------------------------------------------------------


def find_cuad_json(root: Path) -> Path:
    if root.is_file():
        return root
    if not root.is_dir():
        die(f"--cuad path does not exist: {root}")
    for pattern in ("CUAD_v1.json", "**/CUAD_v1.json", "**/*.json"):
        matches = sorted(root.glob(pattern))
        if matches:
            return matches[0]
    die(f"no CUAD JSON found under {root} (expected CUAD_v1.json)")


def load_cuad(path: Path) -> Dict[str, List[Dict[str, Any]]]:
    """Return {doc_key: [{"context": str, "gold": [(clause, start, end, text)]}]}."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        die(f"cannot read CUAD annotations at {path}: {exc}")

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        die(f"{path}: expected SQuAD-style CUAD JSON with a top-level 'data' list")

    documents: Dict[str, List[Dict[str, Any]]] = {}
    for entry in data:
        key = normalize_doc_key(entry.get("title") or "")
        if not key:
            continue
        paragraphs = []
        for paragraph in entry.get("paragraphs") or []:
            context = paragraph.get("context") or ""
            gold: List[Tuple[str, int, int, str]] = []
            for qa in paragraph.get("qas") or []:
                clause = category_from_question(qa.get("question", ""))
                if clause is None or qa.get("is_impossible"):
                    continue
                for answer in qa.get("answers") or []:
                    text = answer.get("text") or ""
                    if not text.strip():
                        continue
                    start = answer.get("answer_start")
                    if isinstance(start, int) and context[
                        start : start + len(text)
                    ] == text:
                        gold.append((clause, start, start + len(text), text))
                    else:
                        located = locate(context, text)
                        if located:
                            gold.append((clause, located[0], located[1], text))
                        else:
                            gold.append((clause, -1, -1, text))
            paragraphs.append({"context": context, "gold": gold})
        if paragraphs:
            documents[key] = paragraphs
    return documents


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------


def prf(tp: int, fp: int, fn: int) -> Dict[str, Any]:
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = (
        2 * precision * recall / (precision + recall) if precision + recall else 0.0
    )
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
    }


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare scored findings against CUAD annotations."
    )
    parser.add_argument(
        "--cuad", required=True, help="CUAD directory or CUAD_v1.json path"
    )
    parser.add_argument(
        "--findings", required=True, help="scored.jsonl (or validated.jsonl)"
    )
    parser.add_argument("--output", required=True, help="metrics.json to write")
    parser.add_argument(
        "--iou",
        type=float,
        default=DEFAULT_IOU,
        help=f"minimum span IoU for a match (default {DEFAULT_IOU})",
    )
    parser.add_argument(
        "--fulltext-dir",
        default="",
        help=(
            "optional ingest sidecar directory; lets recall count documents that "
            "produced zero findings"
        ),
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    if not 0 < args.iou <= 1:
        die("--iou must be in (0, 1]")

    cuad_path = find_cuad_json(Path(args.cuad).expanduser())
    documents = load_cuad(cuad_path)
    eprint(f"[bench] loaded {len(documents)} annotated documents from {cuad_path}")

    findings_by_doc: Dict[str, List[Dict[str, Any]]] = {}
    total_findings = 0
    for payload in read_jsonl(Path(args.findings)):
        clause = payload.get("clause_type")
        if clause not in CLAUSE_TYPES:
            continue
        total_findings += 1
        key = normalize_doc_key(payload.get("doc_id") or "")
        findings_by_doc.setdefault(key, []).append(payload)

    evaluated_keys = set(findings_by_doc) & set(documents)
    if args.fulltext_dir:
        sidecar_dir = Path(args.fulltext_dir).expanduser()
        for path in sidecar_dir.glob(f"*{FULLTEXT_SUFFIX}"):
            key = normalize_doc_key(path.stem)
            if key in documents:
                evaluated_keys.add(key)
    else:
        eprint(
            "[bench] note: pass --fulltext-dir so documents that produced zero "
            "findings are included in the recall denominator."
        )

    if not evaluated_keys:
        die(
            "no document overlap between findings and CUAD. doc_id is matched to "
            "the CUAD title after lowercasing and stripping non-alphanumerics; "
            "check that the PDFs came from the CUAD corpus."
        )

    per_clause = {clause: {"tp": 0, "fp": 0, "fn": 0} for clause in CLAUSE_TYPES}
    unlocatable_findings = 0
    unlocatable_gold = 0
    ignored_findings = 0

    for key in sorted(evaluated_keys):
        findings = findings_by_doc.get(key, [])
        paragraphs = documents[key]

        # Place every finding on the CUAD axis; a finding may only match gold in
        # the paragraph where its quote was found.
        placed: List[Tuple[int, int, int, str]] = []  # (para, start, end, clause)
        for finding in findings:
            quote = str(finding.get("quote") or "")
            clause = finding.get("clause_type")
            best: Optional[Tuple[int, int, int]] = None
            for para_index, paragraph in enumerate(paragraphs):
                located = locate(paragraph["context"], quote)
                if located:
                    best = (para_index, located[0], located[1])
                    break
            if best is None:
                unlocatable_findings += 1
                per_clause[clause]["fp"] += 1
                continue
            placed.append((best[0], best[1], best[2], clause))

        gold_items: List[Tuple[int, int, int, str]] = []
        for para_index, paragraph in enumerate(paragraphs):
            for clause, start, end, _text in paragraph["gold"]:
                if start < 0:
                    unlocatable_gold += 1
                    per_clause[clause]["fn"] += 1
                    continue
                gold_items.append((para_index, start, end, clause))

        pairs: List[Tuple[float, int, int]] = []
        for f_index, (f_para, f_start, f_end, f_clause) in enumerate(placed):
            for g_index, (g_para, g_start, g_end, g_clause) in enumerate(gold_items):
                if f_para != g_para or f_clause != g_clause:
                    continue
                score = iou((f_start, f_end), (g_start, g_end))
                if score >= args.iou:
                    pairs.append((score, f_index, g_index))

        pairs.sort(key=lambda item: (-item[0], item[1], item[2]))
        used_findings, used_gold = set(), set()
        for score, f_index, g_index in pairs:
            if f_index in used_findings or g_index in used_gold:
                continue
            used_findings.add(f_index)
            used_gold.add(g_index)
            per_clause[placed[f_index][3]]["tp"] += 1

        for f_index, item in enumerate(placed):
            if f_index not in used_findings:
                per_clause[item[3]]["fp"] += 1
        for g_index, item in enumerate(gold_items):
            if g_index not in used_gold:
                per_clause[item[3]]["fn"] += 1

    ignored_findings = total_findings - sum(
        len(findings_by_doc.get(key, [])) for key in evaluated_keys
    )

    metrics = {
        "cuad_file": str(cuad_path),
        "findings_file": str(args.findings),
        "iou_threshold": args.iou,
        "documents_evaluated": len(evaluated_keys),
        "documents_annotated": len(documents),
        "findings_total": total_findings,
        "findings_outside_evaluated_documents": ignored_findings,
        "unlocatable_findings": unlocatable_findings,
        "unlocatable_gold": unlocatable_gold,
        "per_clause": {
            clause: prf(counts["tp"], counts["fp"], counts["fn"])
            for clause, counts in per_clause.items()
        },
        "overall": prf(
            sum(c["tp"] for c in per_clause.values()),
            sum(c["fp"] for c in per_clause.values()),
            sum(c["fn"] for c in per_clause.values()),
        ),
        "notes": [
            "Spans are compared in CUAD's coordinate space; finding quotes are "
            "located in the CUAD context by whitespace-tolerant search.",
            "unlocatable_findings count as false positives and unlocatable_gold "
            "as misses, so metrics are a lower bound.",
            "Only CUAD categories mapped to the eight clause types are scored.",
        ],
    }
    write_json(Path(args.output), metrics)

    overall = metrics["overall"]
    eprint(
        f"[bench] {len(evaluated_keys)} documents: P={overall['precision']:.3f} "
        f"R={overall['recall']:.3f} F1={overall['f1']:.3f} -> {args.output}"
    )
    for clause in CLAUSE_TYPES:
        scores = metrics["per_clause"][clause]
        if scores["tp"] or scores["fp"] or scores["fn"]:
            eprint(
                f"[bench]   {clause}: P={scores['precision']:.3f} "
                f"R={scores['recall']:.3f} F1={scores['f1']:.3f} "
                f"(tp={scores['tp']} fp={scores['fp']} fn={scores['fn']})"
            )
    if unlocatable_findings:
        eprint(
            f"[bench] {unlocatable_findings} finding(s) could not be located in "
            "the CUAD text (different PDF extraction); counted as false positives"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
