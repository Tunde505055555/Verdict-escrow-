# VerdictEscrow

**An evidence-grounded adjudication primitive for GenLayer.**

`VerdictEscrow` answers one question that no deterministic chain can answer on
its own, and does it with real validator consensus rather than a trusted
backend:

> Did the counterparty actually deliver what the agreement said?

Funds are escrowed per milestone. The provider submits a claim plus evidence
URLs. The network fetches that evidence, rules against the *written acceptance
criteria*, and money moves only if validators agree on the decision — not just
on the shape of a JSON blob.

This is a primitive, not a demo. The domain lives entirely in agreement
parameters (criteria, evidence allowlist, arbiter), so one deployment serves
freelance milestones, bug-bounty payouts, grant tranches, SLA credits, RWA
delivery confirmation or DAO-funded deliverables.

---

## Repository layout

This is a contract-only repository. There is no frontend, no server, no
hosted service — just the Intelligent Contract, its tests, docs, examples and
deployment tooling.

```
contracts/verdict_escrow.py        the Intelligent Contract (single file, no imports beyond the SDK)
tests/direct/                      direct-mode tests: pure consensus logic + VM state transitions
docs/DESIGN.md                     design brief: how it is written, and how to lift the core
examples/freelance_milestone.md    end-to-end walkthrough
examples/bug_bounty.md             same contract, different agreement parameters
scripts/deploy.py                  deploy to Studio / localnet with genlayer-py
requirements.txt, pyproject.toml   tooling only — the contract has no runtime deps
```

## Quick start

**Studio (fastest):** paste `contracts/verdict_escrow.py` into the GenLayer
Studio editor and deploy with **no constructor arguments**. Protocol defaults
(10% appeal bond, 3 rounds, 72h appeal window, 48h review timeout) are compiled
in — Studio encodes omitted integer arguments as `0`, which the constructor
would reject.

**Scripted:**

```bash
pip install -r requirements.txt
export GENLAYER_RPC=http://localhost:4000/api
python scripts/deploy.py
```

**Tests:**

```bash
pytest -q
```

Then follow `examples/freelance_milestone.md`.

---



## Why this is not "an LLM decides X"

A thin LLM wrapper asks a model for an answer and stores it. The hard part on
GenLayer is making a subjective judgment *converge across independent nodes*
without collapsing into either "any answer goes" or "no answer ever passes".
Four design decisions do that work here.

### 1. The decision is reduced to comparable fields

Every ruling is normalized into:

| Field | Compared? | Why |
| --- | --- | --- |
| `verdict` (3-valued enum) | exact match, always | this is the field that moves money |
| `confidence` (0-100) | per-round tolerance band | LLM scores never match exactly |
| `unmet_criteria` (criterion **indices**) | per-round Jaccard threshold | two nodes must reject for the *same reasons* |
| `rationale` (prose) | not compared programmatically | prose never matches; judged by LLM on appeal |

Criteria are an ordered list, so disagreement is expressed as a set of small
integers. That is what makes "did the validators actually agree?" a
programmatic question instead of a vibe check.

`_normalize_ruling` also enforces self-consistency: `APPROVE` may not list
unmet criteria, `REJECT` must cite at least one. An incoherent ruling is
rejected at the source rather than stored.

### 2. Validator strictness is tiered by appeal round

Escalation is part of the consensus rule, not an off-chain process.

| Round | Confidence tolerance | Unmet-set Jaccard | LLM rationale check |
| --- | --- | --- | --- |
| 1 (first ruling) | ±20 | ≥ 0.50 | no |
| 2 (first appeal) | ±12 | ≥ 0.75 | yes (validator-side prompt) |
| 3 (second appeal) | ±6 | = 1.00 | yes (validator-side prompt) |

Round 1 is cheap and tolerant, because most milestones are uncontested.
Each appeal narrows the band, and from round 2 the validator additionally
asks its own model whether the leader's rationale and its own rest on the
same decisive facts. That check runs inside the validator half of the
non-deterministic block, so its answer is a local boolean and never has to
converge across nodes — it only decides whether *this* validator accepts.

Contested value therefore requires progressively stronger inter-validator
agreement before it moves — the opposite of a system where a stubborn party
can grind out a lucky roll.

### 3. Evidence is treated as hostile input

- **Deterministic domain allowlist.** URLs are checked in ordinary contract
  code *before* any node touches the network, so a provider can never steer
  validators at an arbitrary host. The check is https-only and rejects
  userinfo, explicit ports and suffix look-alikes (`notgithub.com` does not
  match `github.com`).
- **Fenced, capped bodies.** Each response is truncated and wrapped in
  `<<<EVIDENCE id=… >>>` markers.
- **Instruction ordering.** The real instructions come *after* the data, and
  the model is told that anything inside the markers is data. If fetched
  content tries to direct the verdict, the model is instructed to treat that
  as evidence of manipulation and count the affected criteria as unmet.
- **Fetch failure is a fact, not a crash.** A dead link produces
  `FETCH_FAILED` in the prompt and steers the ruling toward
  `INSUFFICIENT_EVIDENCE`.

### 4. It cannot hang, and it cannot go insolvent

- `adjudicate` refuses to run past `max_rounds`. The losing party may then
  post one escalation bond and hand the milestone to the arbiter named at
  agreement creation. The arbiter has power in **no other state**, so the
  primitive does not quietly degrade into a trusted third party.
- `INSUFFICIENT_EVIDENCE` is a real third outcome: nobody loses, the provider
  gets another attempt (up to `MAX_SUBMISSIONS`), bonds are returned.
- `cancel_overdue` lets the payer reclaim capital a provider never claimed.
- `resolve_stalled_review` is the liveness escape hatch for the one state that
  otherwise has no exit without a verdict. If adjudication never produces a
  ruling — validators never satisfy the round rule, an evidence host stays
  down, nobody cranks `adjudicate` — the milestone would sit in `UNDER_REVIEW`
  with funds frozen. After `review_timeout_hours` (48h) from the moment review
  began, the payer, provider or arbiter (nobody else) may call it:
  - round 1, attempts remaining -> back to `AWAITING_EVIDENCE`, bonds returned;
  - round 1, attempts exhausted -> payer refunded, bonds returned;
  - appeal round (2+) -> `DEADLOCKED`, settled by the named arbiter, bonds stay
    escrowed so appealing is still costly.
  It never invents a verdict, and no path lets one party profit from stalling.
- Bonds are tracked **per party**, because the losing side can change between
  rounds. A single "appeal bond" slot would strand the earlier bond and leave
  the escrow short.
- Payouts are **pull, not push** (`credits` ledger + `withdraw`), so a hostile
  recipient cannot block or re-enter settlement.

**Invariant:** at all times `agreement.escrowed` equals the sum of unsettled
milestone amounts plus all posted bonds.

---

## State machine

```text
                        add_milestone
                             │
                          [DRAFT]
                             │ fund_and_activate (exact total)
                             ▼
                  ┌── [AWAITING_EVIDENCE] ──cancel_overdue──▶ [REFUNDED]
                  │          │ submit_evidence
                  │          ▼
                  │    [UNDER_REVIEW] ──adjudicate──▶ [RULED]
                  │          │ resolve_stalled_review (48h, no ruling):
                  │          │   round 1 ▶ AWAITING_EVIDENCE / REFUNDED
                  │          │   round 2+ ▶ DEADLOCKED
                  │          ▲                          │
                  │          │ appeal (bond,            │ accept_ruling
                  │          │  round < max_rounds)     │ or finalize
                  │          └──────────────────────────┤  (window elapsed)
                  │                                     │
                  │  INSUFFICIENT_EVIDENCE              ├──▶ [RELEASED]  APPROVE
                  └─────────(attempts remain)◀──────────┤
                                                        ├──▶ [REFUNDED]  REJECT
                                                        │
                             escalate_to_arbiter (bond, round == max_rounds)
                                                        ▼
                                                  [DEADLOCKED]
                                                        │ arbiter_ruling
                                                        ▼
                                             [RELEASED] / [REFUNDED]
```

---

## Consensus patterns used

| Pattern | Where | Why that one |
| --- | --- | --- |
| `gl.eq_principle.strict_eq` | `_now_hour()` | time bucketed to the hour is exactly reproducible |
| `gl.vm.run_nondet_unsafe` | `_rule_on_milestone()` | the tiered rule needs full control over comparison |
| `gl.nondet.exec_prompt` | validator, rounds ≥ 2 | validator-local semantic comparison of reasoning |

### Time without a clock

Wall-clock time is non-deterministic, so it goes through the equivalence
principle like any other external read: fetch UTC, floor to the hour,
`strict_eq`. Every honest node executing in the same hour returns the same
integer.

**Caveat:** a transaction straddling an hour boundary can fail to converge and
must be retried. Hour granularity is deliberate — every deadline in this
contract (`due_hour`, `appeal_window_hours`) is measured in hours, so finer
resolution would buy nothing and fail more often. If you need minute
granularity, swap the bucket size and widen your deadlines accordingly.

---

## API

**Setup (payer)**
- `create_agreement(provider, arbiter, title, allowed_domains) -> u64`
- `add_milestone(agreement_id, title, criteria[], amount, due_hour) -> u32`
- `fund_and_activate(agreement_id)` — payable, must send the exact total

**Delivery**
- `submit_evidence(agreement_id, index, claim, evidence_urls[])` — provider only
- `adjudicate(agreement_id, index)` — permissionless, runs one round

**Resolution**
- `appeal(agreement_id, index, grounds)` — payable, losing party, round < max
- `escalate_to_arbiter(agreement_id, index, grounds)` — payable, round == max
- `arbiter_ruling(agreement_id, index, approve, reason)` — arbiter, DEADLOCKED only
- `accept_ruling(agreement_id, index)` — losing party waives the window
- `finalize(agreement_id, index)` — anyone, after the window
- `cancel_overdue(agreement_id, index)` — payer, past `due_hour`
- `resolve_stalled_review(agreement_id, index)` — payer/provider/arbiter, after
  `review_timeout_hours` in UNDER_REVIEW with no ruling
- `withdraw()` — pull payment

**Views**
- `get_agreement`, `get_milestone`, `get_rulings`, `credit_of`, `config`

**Constructor**
`VerdictEscrow()` — deploy without arguments. The contract initializes a 10%
appeal bond, 3 adjudication rounds, and a 72-hour appeal window internally so
Studio cannot replace omitted numeric defaults with zero.

---

## Writing good criteria

The consensus quality of this contract is bounded by criteria quality. The
contract enforces atomicity mechanically (1–12 criteria, ≥ 8 chars each), but
the real rule is:

| Bad | Good |
| --- | --- |
| "The work must be high quality" | "The changelog page lists a `1.2` entry dated on or before the due date" |
| "Docs updated" | "The API reference documents every endpoint added in `1.2`" |
| "Site is fast" | "The linked Lighthouse report shows a performance score ≥ 90" |

A criterion should be checkable from the evidence by two independent readers
who reach the same answer. If it is not, validators will diverge and the
milestone will drift to `INSUFFICIENT_EVIDENCE` — which is the correct,
safe failure mode, but not a productive one.

---

## Quickstart

```bash
pip install genlayer-test genvm-linter

genvm-lint check genlayer/contracts/verdict_escrow.py
pytest genlayer/tests/direct/ -v
```

Or paste `contracts/verdict_escrow.py` into
[studio.genlayer.com](https://studio.genlayer.com) and deploy.

---

## Test matrix

`genlayer/tests/direct/test_verdict_escrow.py`

**Pure consensus logic (no VM)**
- domain allowlist: exact, subdomain, look-alike suffix, non-https, userinfo, port
- ruling normalization: verdict canonicalization, confidence clamping, index
  dedupe/sort/bounds, incoherent `APPROVE`, unsupported `REJECT`
- JSON extraction from fenced and prose-wrapped model output
- the tiered acceptance rule: verdict mismatch, tolerated vs rejected score
  drift per round, partial/disjoint/identical reason sets per round

**Contract behaviour (direct mode, mocked web + LLM)**
- happy path: fund → submit → adjudicate → accept → withdraw
- reject refunds the payer
- allowlist enforcement and provider-only submission
- exact-funding requirement
- successful appeal returns the bond; failed appeal forfeits it to the opponent
- only the losing party may appeal
- `INSUFFICIENT_EVIDENCE` returns the milestone for resubmission, escrow intact
- overdue reclaim by the payer
- deadlock → arbiter settlement, and non-arbiter rejection

Tier 4 (the rationale comparison) needs a live model and is exercised in
Studio mode rather than direct mode.

---

## Limitations

- Adjudication quality is bounded by criteria quality and by what the evidence
  page actually contains. This contract makes disagreement *visible and
  bounded*; it does not make ambiguity disappear.
- Evidence is fetched at adjudication time. A page that changes between the
  leader's and validators' fetches will surface as non-convergence, which is
  the safe direction but costs a round. Prefer immutable evidence URLs
  (release tags, permalinks, content-addressed storage).
- Only the milestone total is escrowed; bonds are posted on demand, so a party
  that cannot fund a bond cannot appeal. Tune `appeal_bond_bps` for your
  population.

---

## Lifting the consensus core into your own contract

The reusable part is three functions and one convention. Escrow, milestones and
bonds are scaffolding; replace them freely.

**1. Make your decision comparable.** Reduce whatever your contract judges to a
small enum plus a set of integers. Prose never converges; indices do.

```python
{"verdict": "APPROVE|REJECT|INSUFFICIENT_EVIDENCE",
 "confidence": 0-100,
 "unmet_criteria": [0, 2],      # indices into an ordered criteria list
 "rationale": "..."}            # never compared programmatically
```

**2. Copy `_normalize_ruling`.** It canonicalizes the verdict, clamps the score,
dedupes/sorts/bounds the indices, and rejects self-contradictory output
(`APPROVE` with unmet criteria, `REJECT` with none). Change the enum names and
the criteria bound; leave the coherence checks alone — they are what keeps a
malformed model response out of storage.

**3. Copy the tiered acceptance rule.** Inside
`gl.vm.run_nondet_unsafe(leader_fn, validator_fn)`, the validator compares its
own ruling to the leader's with a strictness that depends on the round:

```python
TIERS = {1: (20, 0.50, False),   # (confidence tolerance, Jaccard floor, LLM check)
         2: (12, 0.75, True),
         3: (6,  1.00, True)}
```

Verdict must match exactly at every tier. Tune the numbers to your stakes — a
low-value, high-volume contract can live at tier 1 forever; a contract settling
large sums should start at tier 2. The round counter can be anything that
escalates: appeal count, value at risk, or a caller-paid strictness flag.

**4. Keep the LLM check validator-local.** The tier-3+ rationale comparison runs
in the *validator* half of the block, so its answer is a local boolean deciding
only whether this validator accepts. It never has to converge across nodes,
which is why an unstable model response degrades into a retry rather than a
chain-wide non-convergence.

**5. Reuse `_is_allowed_url` verbatim** if you fetch anything. It is ordinary
deterministic contract code that runs *before* any node touches the network:
https-only, no userinfo, no explicit port, exact-or-subdomain match so
`notgithub.com` fails against `github.com`. Pair it with the fencing
convention — data first inside `<<<EVIDENCE id=… >>>` markers, instructions
after, and an explicit statement that marker contents are data.

Swap points, in rough order of likelihood: the criteria enum, the tier table,
the prompt body, the settlement effects of each verdict, and the time bucket
size in `_now_hour()`.

---

## Watching a round narrow in Studio

To see the tiering do its work rather than take the table's word for it:

1. Deploy, then `create_agreement` with two criteria — one clearly checkable
   from your evidence URL, one deliberately borderline.
2. `add_milestone`, `fund_and_activate`, `submit_evidence`.
3. Call `adjudicate`. Open the transaction in Studio and read the validator
   outputs: at round 1 you will typically see confidence values several points
   apart and still an accepted ruling — that is the ±20 band absorbing normal
   model variance.
4. Have the losing party `appeal`, then `adjudicate` again. Round 2 applies
   ±12 / Jaccard ≥ 0.75 *and* runs the validator-side rationale prompt. On a
   borderline criterion this is where you see either a genuinely stronger
   agreement or a validator declining to accept.
5. Appeal once more and the round-3 band (±6, identical reason sets) usually
   either settles the milestone decisively or pushes it to
   `INSUFFICIENT_EVIDENCE` / `DEADLOCKED` — both of which are correct outcomes
   for a claim that two independent readers cannot agree on.

Tier 4 (the rationale prompt) only executes with a live model, so this Studio
walkthrough — not the direct-mode suite — is the way to exercise it.

---

MIT licensed. Lift `_rule_on_milestone`, `_normalize_ruling` and the tiered
validator rule into your own contract — that is the reusable part.
