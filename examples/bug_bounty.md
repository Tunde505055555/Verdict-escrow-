# Example: bug-bounty payout

Same contract, no code changes — only different agreement parameters. This is
the point of the primitive: the domain lives in the agreement, not the source.

```python
agreement_id = contract.open_agreement(
    provider="0xResearcher...",
    arbiter="0xSecurityCouncil...",
    title="Critical severity report #482",
    allowed_domains=["hackerone.com", "github.com", "gist.github.com"],
)

contract.add_milestone(
    agreement_id=agreement_id,
    amount=...,  # bounty, escrowed up front
    criteria=[
        "The report describes a reproducible vulnerability in the named repository.",
        "A working proof-of-concept is included.",
        "The issue is not a duplicate of a report filed before this agreement.",
        "Severity as described is Critical under CVSS 3.1.",
    ],
    deadline_hours=168,
)
```

## Why escrow-first matters here

Bounty programmes fail on trust: researchers disclose, then wait on a
discretionary payout. Here the bounty is locked before disclosure, and the
release condition is a network ruling against published criteria rather than a
programme manager's mood. The `arbiter` is a security council, used only if the
network cannot converge after the bounded appeal rounds.

## Notes specific to this use case

* Keep the allowlist tight. Evidence that lives behind auth cannot be fetched
  by validators; publish a redacted public artefact instead.
* Criterion 3 ("not a duplicate") is a judgement call — expect it to be the
  one that drives appeals. That is exactly the case tiered strictness exists
  for: round 1 tolerates a spread, later rounds require the validators to
  reject for the *same indices* and, from round 2, for materially the same
  stated reason.
* Severity criteria should cite a rubric (CVSS here). Criteria that reference
  an external, stable standard converge far better than adjectives.
