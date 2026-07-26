# Clause taxonomy

Load this file when classifying a candidate or filling in `terms`. Exactly eight
clause types exist. If a provision does not fit one of them, it is not a finding
for this review — do not invent a ninth type.

Each section has three parts:

- **Is** / **Is not** — the judgment call, stated as narrowly as possible.
- **`terms` keys** — the exact key names to emit. Omit a key when the quoted span
  does not state it. Never guess a number.
- A fenced ` ```regex ` block — the trigger vocabulary `prefilter.py` parses out
  of this file. Patterns are matched case-insensitively against chunk text.
  They are deliberately over-broad: their job is recall, not precision.

**Editing the regex blocks changes prefilter behaviour.** One pattern per line,
`#` comments and blank lines ignored. Adding a noisy pattern costs model calls;
removing one silently loses findings.

---

## `change_of_control`

**Extract:** trigger definition, consent requirement, notice period, termination
right.

**Is:** a provision whose operation is conditioned on a change in ownership or
control of a party — consent needed, notice owed, or the counterparty gaining a
termination or repricing right.

**Is not:** a passing mention of ownership. Definitions of "Affiliate" or
"Subsidiary", IP ownership clauses, recitals describing corporate structure, and
"controlling" used as a verb all trip the regex and are not findings. The test:
does a change in control *do something* to the parties' rights?

**`terms` keys**

| key | type | meaning |
|---|---|---|
| `trigger` | string | what constitutes the change of control, in the contract's own terms |
| `consent_required` | bool | counterparty consent needed before/upon the event |
| `notice_days` | int | days of notice owed |
| `termination_right` | bool | counterparty may terminate on the event |

```regex
change\s+(?:of|in)\s+control
change\s+(?:of|in)\s+ownership
changes?\s+in\s+(?:the\s+)?(?:beneficial\s+)?ownership
merger,?\s+consolidation
consolidation,?\s+(?:or\s+)?merger
sale\s+of\s+(?:all\s+or\s+)?substantially\s+all
transfer\s+of\s+(?:a\s+)?(?:majority|controlling)\s+(?:interest|stake|share)
acquisition\s+of\s+(?:beneficial\s+)?ownership
(?:more|less)\s+than\s+(?:fifty|50)\s*(?:%|percent)
voting\s+(?:securities|stock|power|interest)
reorganization
```

---

## `anti_assignment`

**Extract:** whether assignment is barred, the consent standard, exceptions.

**Is:** a restriction on transferring the agreement or rights/obligations under
it, including by operation of law.

**Is not:** a bare "successors and assigns" binding clause with no restriction —
that is boilerplate. Also not an assignment of intellectual property or of
receivables, which concern property rather than the contract.

**`terms` keys**

| key | type | meaning |
|---|---|---|
| `assignment_barred` | bool | assignment prohibited outright |
| `consent_required` | bool | consent needed to assign |
| `consent_standard` | string | e.g. `not to be unreasonably withheld`, `sole discretion` |
| `exceptions` | string | carve-outs, e.g. affiliate transfers, transfers with a business unit |
| `survives_assignment` | bool | obligations bind an assignee |

```regex
assign(?:ment|ed|s|ing)?\b
(?:may|shall|will|can)\s+not\s+(?:be\s+)?assign
no\s+(?:party|party\s+may)\s+.{0,40}assign
delegat(?:e|es|ed|ion)
transfer\s+(?:this|the)\s+(?:agreement|contract)
by\s+operation\s+of\s+law
successors?\s+and\s+assigns
novation
prior\s+written\s+consent
```

---

## `mfn`

**Extract:** scope, comparator set, adjustment mechanism.

**Is:** a promise that this counterparty's commercial terms are no worse than
those given to some comparator group, with a mechanism to true up if they are.

**Is not:** a volume discount schedule, a price cap, or a general
non-discrimination statement with no comparator set. "Best efforts" is not
"best price".

**`terms` keys**

| key | type | meaning |
|---|---|---|
| `scope` | string | what the MFN covers (price, all terms, specific SKUs) |
| `comparator_set` | string | which customers/counterparties are compared |
| `adjustment_mechanism` | string | automatic reprice, credit, refund, renegotiation |

```regex
most\s+favo(?:u)?red\s+(?:nation|customer|pricing|treatment)
\bMFN\b
no\s+less\s+favo(?:u)?rable
(?:no\s+)?more\s+favo(?:u)?rable\s+(?:price|prices|pricing|terms|rates)
best\s+(?:price|prices|pricing|terms|rates)\s+(?:offered|available|provided)
price\s+parity
lowest\s+price
comparable\s+customers?
```

---

## `indemnity_cap`

**Extract:** cap amount or formula, carve-outs, whether uncapped.

**Is:** the monetary ceiling on liability or indemnity obligations, or the
explicit absence of one.

**Is not:** the indemnity trigger itself (who indemnifies whom for what) unless a
cap or an express absence of a cap appears in the same span. A disclaimer of
consequential damages is not a cap.

**Reading a formula:** a cap stated as a formula (`the fees paid in the twelve
months preceding the claim`) is not a number. Put the formula verbatim in
`cap_formula` and leave `cap_amount` absent. Do not compute a value — the inputs
are not in the document. Note in `summary` what the formula resolves against.

**`terms` keys**

| key | type | meaning |
|---|---|---|
| `uncapped` | bool | true when liability is expressly unlimited |
| `cap_amount` | number | a stated absolute figure only |
| `cap_formula` | string | verbatim formula when the cap is derived |
| `carve_outs` | string | obligations excluded from the cap (IP, confidentiality, gross negligence) |

```regex
indemnif(?:y|ies|ied|ication)
hold\s+harmless
limitation\s+of\s+liability
limit(?:s|ation)?\s+of\s+liability
(?:aggregate\s+)?liability\s+(?:shall|will|does)\s+not\s+exceed
shall\s+not\s+exceed
in\s+no\s+event\s+shall
total\s+(?:aggregate\s+)?liability
unlimited\s+liability
without\s+limitation\s+as\s+to\s+amount
cap(?:ped)?\s+(?:on|at)\b
consequential\s+damages
```

---

## `auto_renewal`

**Extract:** renewal term, notice window to prevent, price escalator.

**Is:** a term that extends itself unless a party acts, and the mechanics of
acting in time.

**Is not:** a renewal that requires affirmative agreement ("the parties may
renew by mutual written consent"). That is an option, not an auto-renewal.

**`terms` keys**

| key | type | meaning |
|---|---|---|
| `renewal_term` | string | length of each renewal period |
| `notice_window_days` | int | days before expiry that non-renewal notice is due |
| `price_escalator` | string | uplift applied on renewal |

```regex
automatically\s+renew
auto[-\s]?renew(?:al|s|ed)?
renewal\s+term
successive\s+(?:one|two|three|1|2|3)[-\s]?(?:year|month)\s+(?:term|terms|period|periods)
additional\s+(?:one|two|1|2)[-\s]?year\s+(?:term|period)
unless\s+(?:either\s+party|a\s+party)\s+.{0,60}notice
shall\s+be\s+extended
evergreen
initial\s+term\s+.{0,40}thereafter
```

---

## `termination_convenience`

**Extract:** which party, notice period, fees.

**Is:** a right to terminate without breach or cause.

**Is not:** termination for cause, for insolvency, or for uncured breach. A
notice-period regex hit inside a cure provision is not this clause.

**`terms` keys**

| key | type | meaning |
|---|---|---|
| `party` | string | `customer`, `supplier`, `either`, or the named party |
| `notice_days` | int | notice required to exercise |
| `fees` | string | early termination fee or wind-down cost |

```regex
terminate\s+(?:this\s+agreement\s+)?(?:at\s+any\s+time\s+)?for\s+convenience
for\s+(?:its\s+)?convenience
terminate\s+.{0,40}for\s+any\s+reason
without\s+cause
with\s+or\s+without\s+cause
terminate\s+.{0,30}upon\s+(?:not\s+less\s+than\s+)?\w+\s*\(?\d*\)?\s*days
early\s+termination\s+(?:fee|charge|payment)
termination\s+for\s+convenience
```

---

## `exclusivity`

**Extract:** scope, territory, duration.

**Is:** a restriction on dealing with third parties — exclusive supply or
distribution, non-competes, minimum-volume commitments that operate as
exclusivity, rights of first refusal.

**Is not:** a non-solicitation of employees, or confidentiality. Non-exclusive
grants ("a non-exclusive, worldwide licence") trip the regex and are the
opposite of a finding.

**`terms` keys**

| key | type | meaning |
|---|---|---|
| `scope` | string | products, services, or customers covered |
| `territory` | string | geographic reach |
| `duration` | string | how long the restriction runs, including post-term tails |

```regex
exclusiv(?:e|ely|ity)
sole\s+(?:and\s+exclusive\s+)?(?:supplier|provider|distributor|source|vendor)
sole\s+source
shall\s+not\s+(?:directly\s+or\s+indirectly\s+)?(?:engage|compete|sell|distribute)
non[-\s]?compet(?:e|ition|itive)
right\s+of\s+first\s+(?:refusal|offer|negotiation)
minimum\s+(?:purchase|volume|order)\s+(?:commitment|requirement|quantity)
requirements\s+contract
restricted\s+(?:territory|customers)
```

---

## `governing_law`

**Extract:** jurisdiction, venue, arbitration.

**Is:** the law governing the agreement, the forum for disputes, and any
mandatory arbitration.

**Is not:** a choice-of-law reference inside a definition (e.g. "organized under
the laws of Delaware"), or a compliance-with-laws covenant.

**`terms` keys**

| key | type | meaning |
|---|---|---|
| `jurisdiction` | string | governing law |
| `venue` | string | courts or seat of arbitration |
| `arbitration` | string | rules and administering body, or absent |

```regex
govern(?:ed|ing)\s+by\s+(?:and\s+construed\s+)?(?:in\s+accordance\s+with\s+)?the\s+laws
governing\s+law
choice\s+of\s+law
exclusive\s+jurisdiction
submit\s+to\s+the\s+(?:exclusive\s+)?jurisdiction
venue\s+(?:shall|will)\s+(?:be|lie)
courts\s+(?:located\s+)?in
arbitrat(?:ion|e|ed)
\bJAMS\b
\bAAA\b
\bICC\b
\bUNCITRAL\b
waiver\s+of\s+jury\s+trial
```
