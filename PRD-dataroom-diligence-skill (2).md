# Build Spec — `dataroom-diligence` Skill Files

**Consumer:** an AI coding agent (Claude Code / Cursor)
**Task:** write the skill directory. Nothing else — no tests, no fixtures, no installation.
Testing and Hermes integration happen tomorrow on the target machine.
**Status:** authoritative. Where this conflicts with your priors, follow this.

---

## 1. Objective

A skill directory that reviews a folder of commercial contracts for M&A due-diligence issues and
emits a ranked issues list where every citation is verified against source text.

The scripts run as a plain CLI pipeline. Nothing imports an agent framework. Tomorrow the same
directory drops into `~/.hermes/skills/` unchanged.

---

## 2. Target environment

Deployment is a Dell Pro Max with NVIDIA GB10: 20-core **aarch64**, 128 GB unified memory,
`Nemotron-3-Nano-30B-A3B-NVFP4` served by vLLM, inside an OpenShell sandbox with default-deny
egress, driven by Hermes.

### Two execution contexts — the scripts must work in both

| Context | Who runs it | Model endpoint | Package installs |
|---|---|---|---|
| **Host** | you, from a shell | `http://localhost:8000/v1` | possible |
| **Sandbox** | Hermes, via the skill | `https://inference.local/v1` | **impossible** |

Inside the sandbox, `localhost` reaches nothing. OpenShell intercepts traffic to
`inference.local` and forwards it to the host-side server. Two rules follow, and violating either
means the skill fails on first run:

- **`inference.local` must be `https://`.** The proxy only intercepts HTTPS on that hostname.
  Never bypass the proxy or target a host IP — it will hang until timeout.
- **Nothing may be installed at runtime.** Default-deny egress means no PyPI. Dependencies are
  pre-staged from a local wheelhouse before the skill ever runs.

Further consequences:

- Pure-Python dependencies where possible; every one is a thing that must be pre-staged for
  aarch64. Pin versions in `requirements.txt`. Do not vendor wheels or commit a virtualenv —
  architecture differs from the dev machine.
- No hardcoded endpoints, model IDs, or absolute paths. CLI flag or env var, with defaults.
- No network calls except to the configured model endpoint.
- Assume 30–60 tok/s single-stream. Keep model outputs short and structured.

---

## 3. Deliverable

```
dataroom-diligence/
├── SKILL.md
├── README.md               # how to run the pipeline standalone
├── requirements.txt
├── scripts/
│   ├── schemas.py
│   ├── ingest.py
│   ├── prefilter.py
│   ├── sweep.py
│   ├── validate.py
│   ├── score.py
│   ├── export.py
│   └── bench.py
├── references/
│   ├── clause-taxonomy.md
│   └── severity-rules.md
└── templates/
    └── redflag-memo.md
```

**Division of responsibility — the architectural core:**

- `scripts/` do mechanical work only: parse bytes, match patterns, compare strings, write files.
  **A script never makes a legal determination.**
- The model does judgment: is this actually a change-of-control clause, what does the cap resolve
  to, how severe is it.
- `SKILL.md` encodes the procedure and guardrails for whichever agent runs it later.

If you find yourself writing clause *classification* into `prefilter.py`, stop. Prefilter returns
candidates, not findings.

---

## 4. Core invariant

> No finding may be reported unless its `quote` appears verbatim in the source document at the
> stated page and character offsets.

`validate.py` enforces this by exact string comparison against ingested source text. No fuzzy
matching, no normalization beyond whitespace collapse. Failures are dropped and counted, never
repaired. Enforced in code, never in a prompt.

---

## 5. Schemas

Define once as dataclasses in `scripts/schemas.py`, import everywhere. All intermediate files are
JSONL, one object per line.

### Chunk — from `ingest.py`

```json
{
  "chunk_id": "stable hash",
  "doc_id": "source filename stem",
  "doc_title": "best-effort from first page",
  "page": 12,
  "char_start": 4021,
  "char_end": 5533,
  "text": "raw extracted text for this span"
}
```

Offsets are relative to **full-document** text, not the page. Write per-document full text to a
sidecar directory so `validate.py` can verify independently.

### Candidate — from `prefilter.py`

```json
{
  "candidate_id": "string",
  "chunk_id": "string",
  "doc_id": "string",
  "page": 12,
  "char_start": 4021,
  "char_end": 5533,
  "text": "the candidate window",
  "triggers": ["change of control", "assign"],
  "clause_hints": ["change_of_control", "anti_assignment"]
}
```

`clause_hints` is a suggestion to the model, never a conclusion.

### Finding — from `sweep.py`, enriched downstream

```json
{
  "finding_id": "string",
  "candidate_id": "string",
  "doc_id": "string",
  "doc_title": "string",
  "clause_type": "change_of_control",
  "page": 12,
  "char_start": 4102,
  "char_end": 4310,
  "quote": "verbatim span, <= 400 chars",
  "summary": "one sentence in the model's own words",
  "terms": {
    "consent_required": true,
    "notice_days": 30,
    "cap_amount": null,
    "cap_formula": "12 months of fees paid",
    "survives_assignment": false
  },
  "severity": "blocking",
  "confidence": 0.82,
  "validated": true
}
```

`terms` is clause-type-dependent and may be sparse. `severity` and `validated` are set
downstream, never by the model.

---

## 6. Clause taxonomy

Exactly these eight. Do not add more.

| `clause_type` | Extract |
|---|---|
| `change_of_control` | Trigger definition, consent requirement, notice period, termination right |
| `anti_assignment` | Whether assignment is barred, consent standard, exceptions |
| `mfn` | Scope, comparator set, adjustment mechanism |
| `indemnity_cap` | Cap amount or formula, carve-outs, whether uncapped |
| `auto_renewal` | Renewal term, notice window to prevent, price escalator |
| `termination_convenience` | Which party, notice period, fees |
| `exclusivity` | Scope, territory, duration |
| `governing_law` | Jurisdiction, venue, arbitration |

Definitions and regex trigger vocabulary go in `references/clause-taxonomy.md`.

**Severity** (`references/severity-rules.md`), parameterized by deal structure:

- `blocking` — deal cannot close without resolution
- `consent_required` — counterparty consent needed, adds timeline risk
- `note` — worth flagging, not gating

Asset deals trip `anti_assignment` where stock deals do not. Stock deals trip
`change_of_control` where asset deals may not. `score.py` takes `--deal-structure {stock,asset}`
and reorders accordingly. Build this — it's a demo centerpiece.

---

## 7. Script contracts

Every script: standalone CLI, `argparse`, `--help`, exit 0 on success, non-zero with a stderr
message otherwise. Nothing imports another script except `schemas.py`. Only `sweep.py` touches
the network.

```
ingest.py     --input DIR --output chunks.jsonl --fulltext-dir DIR
prefilter.py  --input chunks.jsonl --output candidates.jsonl [--clauses LIST]
sweep.py      --candidates candidates.jsonl --output findings.jsonl
              [--endpoint URL] [--model ID] [--workers N] [--limit N]
validate.py   --findings findings.jsonl --fulltext-dir DIR --output validated.jsonl
              [--report validation-report.json]
score.py      --input validated.jsonl --deal-structure {stock,asset} --output scored.jsonl
export.py     --input scored.jsonl --xlsx issues.xlsx --memo redflag-memo.md
bench.py      --cuad CUAD_DIR --findings scored.jsonl --output metrics.json
```

**`ingest.py`** — PyMuPDF. Chunk on paragraph boundaries, ~1500 chars with 200-char overlap.
Preserve exact offsets. Write per-document full text to `--fulltext-dir`.

**`prefilter.py`** — case-insensitive regex over chunk text, vocabulary from
`references/clause-taxonomy.md`. High-recall and unashamedly noisy: aim to drop ~90% of chunks
while keeping essentially all true positives. Precision is the model's job.

**`sweep.py`** — plain HTTP against an OpenAI-compatible `/v1/chat/completions` endpoint via
`httpx`. Do not import Hermes, `run_agent`, or any agent framework.

```python
# Default targets the sandbox route. For host-side runs pass
# --endpoint http://localhost:8000/v1
DEFAULT_ENDPOINT = os.environ.get("DILIGENCE_ENDPOINT", "https://inference.local/v1")
DEFAULT_MODEL    = os.environ.get("DILIGENCE_MODEL", "local-model")
DEFAULT_API_KEY  = os.environ.get("DILIGENCE_API_KEY", "dummy")
```

**Never error on a missing API key.** OpenShell's proxy injects the real credential at egress, so
the sandbox never sees one. Send `DEFAULT_API_KEY` as the bearer token and let the proxy handle it.

**Never disable proxying.** No `--noproxy`, no `trust_env=False`, no direct host IPs. The
`inference.local` hostname resolves only through the OpenShell proxy.

One call per candidate, strict JSON out, `temperature=0`, tight `max_tokens`. Concurrency via
`concurrent.futures.ThreadPoolExecutor`. On malformed JSON: one retry with a repair prompt, then
drop and log — never partially parse. Keep the prompt in a module-level constant so it can be
swapped without touching logic.

**`validate.py`** — load source full text, slice `[char_start:char_end]`, compare to `quote` after
whitespace collapse only. Set `validated`. Write failures separately and report the drop rate.

**`score.py`** — apply severity rules by clause type and deal structure. Pure function of input,
no model.

**`export.py`** — XLSX via `openpyxl`, one row per finding: doc, page, clause type, severity,
quote, summary, terms. Sorted by severity then document. Memo rendered from
`templates/redflag-memo.md`, grouped by clause type.

**`bench.py`** — compare against CUAD annotations. Match on document + clause category + span
overlap (IoU ≥ 0.5). Emit per-clause and overall precision, recall, F1 to `metrics.json`.

---

## 8. SKILL.md

Authored now, installed tomorrow. Write it for a competent agent reading cold.

YAML frontmatter with `name` and `description`. The description is the triggering mechanism —
explicit and slightly pushy, naming the contexts that should fire it: data room, due diligence,
contract review, change of control, red flag memo.

Body, under 500 lines, imperative voice:

1. **When to use** — pointer to a folder of contracts, diligence framing
2. **Setup** — how to install dependencies offline, since the sandbox cannot reach PyPI:
   ```bash
   pip install --no-index --find-links <wheelhouse> -r requirements.txt
   ```
   State plainly that this must be run once before the workflow, and that if imports fail the
   cause is a missing wheelhouse rather than a bug in the scripts.
3. **Workflow** — the exact script sequence, with the guardrail that `validate.py` runs before
   anything is reported
4. **Judgment guidance** — distinguishing a real change-of-control provision from a passing
   mention of ownership; reading a cap expressed as a formula
5. **Output contract** — what a finished run produces and where it lands
6. **Done criteria** — non-zero findings, validation drop rate under threshold, both output files
   written
7. **Pointers** to `references/` — load `clause-taxonomy.md` when classifying,
   `severity-rules.md` when scoring

Do not inline the taxonomy into SKILL.md — the reader pulls references only when needed.
Reference scripts by relative path, never absolute.

---

## 9. Non-goals

- **Tests, fixtures, mock modes, CI.** Not now.
- **Any Hermes integration** — no imports, no `~/.hermes/` paths, no `skill_manage`.
- **Runtime package installation.** No `pip install` from a script, no `subprocess` calls to a
  package manager, no lazy imports that fetch. If a dependency is missing, fail with a clear
  message naming the wheelhouse command. The sandbox has no egress.
- Model serving config, vLLM flags, OpenShell policy
- OCR / scanned documents; any format other than PDF
- Amendment chaining and supersession logic
- Multi-user, auth, persistence beyond flat files
- Model fine-tuning
- Any UI
- Retrieval / vector database — regex prefilter is sufficient at this scale
- Vendored wheels or platform-specific binaries

---

## 10. Hazards

**Offsets are the whole ballgame.** Chunk overlap makes off-by-one errors easy and they silently
break validation. Be deliberate about the arithmetic.

**JSON discipline.** Small models drift from schema under long prompts. Keep the extraction prompt
short, give one worked example, set `temperature=0`, validate every response against the dataclass
before accepting it.

**Prompt injection.** Contract text is attacker-influenceable. Never execute extracted content,
interpolate it into a shell command, or use it to construct a file path.

**Cross-architecture.** Written on one machine, deployed to aarch64. Pin versions, install on
target from a local wheelhouse.

**Sandbox networking is the most likely first-run failure.** Symptom is a hang until timeout, not
an error. Cause is almost always `http://` instead of `https://` on `inference.local`, a bypassed
proxy, or a hardcoded `localhost`. Make the endpoint visible in a startup log line so this is
diagnosable in one glance rather than one hour.

**Keep dependencies minimal.** Every package is one more aarch64 wheel to pre-stage and one more
thing that can fail inside a sealed container. Four is the target: `pymupdf`, `httpx`, `openpyxl`,
and the stdlib. Reach for `pandas` only if `csv` genuinely will not do.
