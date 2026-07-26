# Red Flag Memo — {{MATTER}}

**Deal structure:** {{DEAL_STRUCTURE}}
**Documents reviewed:** {{DOC_COUNT}}
**Findings reported:** {{FINDING_COUNT}}
**Generated:** {{GENERATED_AT}}

Every quotation below was verified character-for-character against the source
PDF at the cited page and character offsets. Findings that failed verification
were dropped, not repaired.

## Summary by severity

{{SEVERITY_TABLE}}

## Summary by clause type

{{CLAUSE_TABLE}}

## Findings

{{FINDINGS_BY_CLAUSE}}

## Verification

{{VALIDATION_NOTE}}

## Scope and limits

- PDF text only. Scanned documents produce no text and were skipped, not read.
- Severity is per clause, not weighted by contract value or materiality.
- Amendments were treated as standalone documents; no supersession chain was
  built, so a superseded clause may appear alongside its replacement.
- Clause coverage is limited to the eight types in
  `references/clause-taxonomy.md`.
- Findings are extraction output for a lawyer to review. This is not advice.
