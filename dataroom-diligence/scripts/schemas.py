"""Shared data contracts for the dataroom-diligence pipeline.

This is the only module other scripts import. Everything here is mechanical:
parsing, hashing, string comparison, file IO. Nothing in this pipeline's
Python makes a legal determination -- that is the model's job.

All intermediate files are JSONL, one object per line, UTF-8.
"""

from __future__ import annotations

import importlib
import json
import re
import sys
from bisect import bisect_right
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, NoReturn, Optional, Tuple

# --------------------------------------------------------------------------
# Vocabulary. Exactly eight clause types. Do not add more.
# --------------------------------------------------------------------------

CLAUSE_TYPES: Tuple[str, ...] = (
    "change_of_control",
    "anti_assignment",
    "mfn",
    "indemnity_cap",
    "auto_renewal",
    "termination_convenience",
    "exclusivity",
    "governing_law",
)

SEVERITIES: Tuple[str, ...] = ("blocking", "consent_required", "note")

# Lower rank sorts first everywhere (xlsx rows, memo sections, scored.jsonl).
SEVERITY_RANK: Dict[str, int] = {name: i for i, name in enumerate(SEVERITIES)}

DEAL_STRUCTURES: Tuple[str, ...] = ("stock", "asset")

# Canonical `terms` keys per clause type. Single source of truth: sweep.py
# injects these into the extraction prompt, score.py reads them, and
# references/clause-taxonomy.md documents them for the reading agent. `terms`
# may be sparse -- a missing key means "not stated in the quoted span".
CLAUSE_TERMS: Dict[str, Tuple[str, ...]] = {
    "change_of_control": (
        "trigger",
        "consent_required",
        "notice_days",
        "termination_right",
    ),
    "anti_assignment": (
        "assignment_barred",
        "consent_required",
        "consent_standard",
        "exceptions",
        "survives_assignment",
    ),
    "mfn": ("scope", "comparator_set", "adjustment_mechanism"),
    "indemnity_cap": ("uncapped", "cap_amount", "cap_formula", "carve_outs"),
    "auto_renewal": ("renewal_term", "notice_window_days", "price_escalator"),
    "termination_convenience": ("party", "notice_days", "fees"),
    "exclusivity": ("scope", "territory", "duration"),
    "governing_law": ("jurisdiction", "venue", "arbitration"),
}

# A citation longer than this is a summary, not a quote.
MAX_QUOTE_CHARS = 400

# Sidecar filenames written by ingest.py and read by validate.py / bench.py.
FULLTEXT_SUFFIX = ".txt"
PAGEMAP_SUFFIX = ".pages.json"


# --------------------------------------------------------------------------
# Dependencies. The sandbox has no egress: never install anything at runtime.
# --------------------------------------------------------------------------

WHEELHOUSE_HINT = (
    "pip install --no-index --find-links <wheelhouse> -r requirements.txt"
)


def require(module: str, package: str, purpose: str = "") -> Any:
    """Import a third-party module or die with an actionable message.

    Never attempts installation. The deployment sandbox is egress-denied, so a
    missing import means the wheelhouse was not staged, not that the script is
    broken.
    """
    try:
        return importlib.import_module(module)
    except ImportError as exc:  # pragma: no cover - environment dependent
        detail = f" ({purpose})" if purpose else ""
        die(
            f"missing dependency '{package}'{detail}: {exc}\n"
            f"install it offline from the pre-staged wheelhouse:\n"
            f"  {WHEELHOUSE_HINT}\n"
            f"nothing may be installed from the network at runtime."
        )


# --------------------------------------------------------------------------
# Small utilities
# --------------------------------------------------------------------------

_WS_RE = re.compile(r"\s+")


def collapse_ws(text: str) -> str:
    """Collapse all whitespace runs to a single space and strip the ends.

    This is the ONLY normalization permitted anywhere in the verification path.
    No case folding, no punctuation stripping, no fuzzy matching.
    """
    return _WS_RE.sub(" ", text).strip()


def stable_id(prefix: str, *parts: Any) -> str:
    """Deterministic short id. Same inputs always yield the same id, so reruns
    are idempotent and duplicate findings from overlapping chunks collapse."""
    import hashlib

    raw = "|".join(str(p) for p in parts)
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{digest}" if prefix else digest


_SAFE_DOC_ID_RE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_doc_id(doc_id: str) -> str:
    """Reduce a doc_id to something that cannot escape a directory.

    Document text is attacker-influenceable and doc_id travels through JSONL
    files, so never build a path from a raw doc_id.
    """
    cleaned = _SAFE_DOC_ID_RE.sub("_", (doc_id or "").strip())
    cleaned = cleaned.strip("._-")
    if not cleaned:
        raise ValueError(f"unusable doc_id: {doc_id!r}")
    return cleaned[:120]


def eprint(*args: Any) -> None:
    print(*args, file=sys.stderr, flush=True)


def die(message: str, code: int = 1) -> NoReturn:
    """Print to stderr and exit with `code`. Every script's failure path."""
    eprint(f"error: {message}")
    raise SystemExit(code)


# --------------------------------------------------------------------------
# JSONL / text IO
# --------------------------------------------------------------------------


def read_jsonl(path: Path) -> Iterator[Dict[str, Any]]:
    path = Path(path)
    if not path.exists():
        die(f"input file not found: {path}")
    with path.open("r", encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                die(f"{path}:{lineno}: malformed JSONL: {exc}")
            if not isinstance(obj, dict):
                die(f"{path}:{lineno}: expected a JSON object per line")
            yield obj


def write_jsonl(path: Path, objects: Iterable[Any]) -> int:
    """Write dicts or dataclasses as JSONL. Returns the number of lines."""
    path = Path(path)
    if path.parent and str(path.parent) not in ("", "."):
        path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for obj in objects:
            payload = obj.to_dict() if hasattr(obj, "to_dict") else obj
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
            count += 1
    return count


def write_json(path: Path, payload: Any) -> None:
    path = Path(path)
    if path.parent and str(path.parent) not in ("", "."):
        path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def fulltext_path(fulltext_dir: Path, doc_id: str) -> Path:
    return Path(fulltext_dir) / f"{safe_doc_id(doc_id)}{FULLTEXT_SUFFIX}"


def pagemap_path(fulltext_dir: Path, doc_id: str) -> Path:
    return Path(fulltext_dir) / f"{safe_doc_id(doc_id)}{PAGEMAP_SUFFIX}"


def write_fulltext(fulltext_dir: Path, doc_id: str, text: str) -> Path:
    """Write document full text byte-for-byte. newline='' disables newline
    translation so offsets computed at ingest survive the round trip."""
    path = fulltext_path(fulltext_dir, doc_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    return path


def read_fulltext(fulltext_dir: Path, doc_id: str) -> Optional[str]:
    path = fulltext_path(fulltext_dir, doc_id)
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8", newline="") as handle:
        return handle.read()


def read_pagemap(fulltext_dir: Path, doc_id: str) -> Optional[List[List[int]]]:
    path = pagemap_path(fulltext_dir, doc_id)
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    spans = data.get("pages") if isinstance(data, dict) else data
    if not isinstance(spans, list):
        return None
    return [[int(s[0]), int(s[1])] for s in spans if isinstance(s, (list, tuple))]


def page_for_offset(page_spans: Iterable[Iterable[int]], offset: int) -> int:
    """1-based page number containing `offset`. Clamps to the last page."""
    starts = [int(span[0]) for span in page_spans]
    if not starts:
        return 1
    idx = bisect_right(starts, offset) - 1
    return max(1, min(len(starts), idx + 1))


# --------------------------------------------------------------------------
# Dataclasses
# --------------------------------------------------------------------------


def _require_keys(payload: Dict[str, Any], keys: Iterable[str], label: str) -> None:
    missing = [k for k in keys if k not in payload]
    if missing:
        raise ValueError(f"{label} missing field(s): {', '.join(missing)}")


@dataclass
class Chunk:
    """A contiguous span of one document's full text. Offsets are relative to
    FULL-DOCUMENT text, never to the page."""

    chunk_id: str
    doc_id: str
    doc_title: str
    page: int
    char_start: int
    char_end: int
    text: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "doc_id": self.doc_id,
            "doc_title": self.doc_title,
            "page": self.page,
            "char_start": self.char_start,
            "char_end": self.char_end,
            "text": self.text,
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "Chunk":
        _require_keys(
            payload,
            ("chunk_id", "doc_id", "page", "char_start", "char_end", "text"),
            "chunk",
        )
        return cls(
            chunk_id=str(payload["chunk_id"]),
            doc_id=str(payload["doc_id"]),
            doc_title=str(payload.get("doc_title") or ""),
            page=int(payload["page"]),
            char_start=int(payload["char_start"]),
            char_end=int(payload["char_end"]),
            text=str(payload["text"]),
        )


@dataclass
class Candidate:
    """A window that tripped at least one regex trigger. `clause_hints` is a
    suggestion to the model, never a conclusion."""

    candidate_id: str
    chunk_id: str
    doc_id: str
    doc_title: str
    page: int
    char_start: int
    char_end: int
    text: str
    triggers: List[str] = field(default_factory=list)
    clause_hints: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "chunk_id": self.chunk_id,
            "doc_id": self.doc_id,
            "doc_title": self.doc_title,
            "page": self.page,
            "char_start": self.char_start,
            "char_end": self.char_end,
            "text": self.text,
            "triggers": list(self.triggers),
            "clause_hints": list(self.clause_hints),
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "Candidate":
        _require_keys(
            payload,
            (
                "candidate_id",
                "chunk_id",
                "doc_id",
                "page",
                "char_start",
                "char_end",
                "text",
            ),
            "candidate",
        )
        return cls(
            candidate_id=str(payload["candidate_id"]),
            chunk_id=str(payload["chunk_id"]),
            doc_id=str(payload["doc_id"]),
            doc_title=str(payload.get("doc_title") or ""),
            page=int(payload["page"]),
            char_start=int(payload["char_start"]),
            char_end=int(payload["char_end"]),
            text=str(payload["text"]),
            triggers=[str(t) for t in payload.get("triggers") or []],
            clause_hints=[str(h) for h in payload.get("clause_hints") or []],
        )


@dataclass
class Finding:
    """One reported issue.

    `severity` and `validated` are set downstream by score.py / validate.py --
    never by the model. Unknown keys survive round trips in `extra` so
    downstream stages can enrich without a schema change here.
    """

    finding_id: str
    candidate_id: str
    doc_id: str
    doc_title: str
    clause_type: str
    page: int
    char_start: int
    char_end: int
    quote: str
    summary: str
    terms: Dict[str, Any] = field(default_factory=dict)
    severity: Optional[str] = None
    confidence: Optional[float] = None
    validated: bool = False
    extra: Dict[str, Any] = field(default_factory=dict)

    _CORE = (
        "finding_id",
        "candidate_id",
        "doc_id",
        "doc_title",
        "clause_type",
        "page",
        "char_start",
        "char_end",
        "quote",
        "summary",
        "terms",
        "severity",
        "confidence",
        "validated",
    )

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "finding_id": self.finding_id,
            "candidate_id": self.candidate_id,
            "doc_id": self.doc_id,
            "doc_title": self.doc_title,
            "clause_type": self.clause_type,
            "page": self.page,
            "char_start": self.char_start,
            "char_end": self.char_end,
            "quote": self.quote,
            "summary": self.summary,
            "terms": dict(self.terms),
            "severity": self.severity,
            "confidence": self.confidence,
            "validated": self.validated,
        }
        for key, value in self.extra.items():
            if key not in payload:
                payload[key] = value
        return payload

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "Finding":
        _require_keys(
            payload,
            (
                "finding_id",
                "doc_id",
                "clause_type",
                "page",
                "char_start",
                "char_end",
                "quote",
            ),
            "finding",
        )
        terms = payload.get("terms") or {}
        if not isinstance(terms, dict):
            raise ValueError("finding.terms must be an object")
        confidence = payload.get("confidence")
        return cls(
            finding_id=str(payload["finding_id"]),
            candidate_id=str(payload.get("candidate_id") or ""),
            doc_id=str(payload["doc_id"]),
            doc_title=str(payload.get("doc_title") or ""),
            clause_type=str(payload["clause_type"]),
            page=int(payload["page"]),
            char_start=int(payload["char_start"]),
            char_end=int(payload["char_end"]),
            quote=str(payload["quote"]),
            summary=str(payload.get("summary") or ""),
            terms=dict(terms),
            severity=payload.get("severity"),
            confidence=None if confidence is None else float(confidence),
            validated=bool(payload.get("validated", False)),
            extra={k: v for k, v in payload.items() if k not in cls._CORE},
        )


# --------------------------------------------------------------------------
# Model-output shape check (used by sweep.py before a response is accepted)
# --------------------------------------------------------------------------


def check_model_finding(payload: Any) -> Tuple[bool, str]:
    """Validate one raw finding object emitted by the model.

    Returns (ok, reason). The model supplies clause_type, quote, summary,
    terms and confidence only -- offsets are resolved mechanically against the
    candidate window, and severity/validated are set downstream.
    """
    if not isinstance(payload, dict):
        return False, "not_an_object"
    clause_type = payload.get("clause_type")
    if clause_type not in CLAUSE_TYPES:
        return False, f"unknown_clause_type:{clause_type!r}"
    quote = payload.get("quote")
    if not isinstance(quote, str) or not quote.strip():
        return False, "missing_quote"
    if len(quote) > MAX_QUOTE_CHARS:
        return False, "quote_too_long"
    summary = payload.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        return False, "missing_summary"
    terms = payload.get("terms", {})
    if terms is not None and not isinstance(terms, dict):
        return False, "terms_not_an_object"
    confidence = payload.get("confidence")
    if confidence is not None:
        try:
            value = float(confidence)
        except (TypeError, ValueError):
            return False, "confidence_not_a_number"
        if not 0.0 <= value <= 1.0:
            return False, "confidence_out_of_range"
    return True, "ok"
