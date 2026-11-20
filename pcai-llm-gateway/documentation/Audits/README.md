# Audit trail

Dated, zone-partitioned audit waves — what each wave found, what was fixed, and
the evidence. They are historical records: the current behaviour is
`FEATURES.md` + the code, not a finding in here.

| Wave | Report | Remediation |
|---|---|---|
| 2026-10-26 | [AUDIT-2026-10-26-relay.md](AUDIT-2026-10-26-relay.md) — relay correctness (reasoning keys, tool-call assembly, render index) | folded into the 10-29 wave |
| 2026-10-28 | [AUDIT-2026-10-28-quality.md](AUDIT-2026-10-28-quality.md) — "the models feel weaker" quality investigation | — |
| 2026-10-29 | [AUDIT-2026-10-29.md](AUDIT-2026-10-29.md) — full-repo wave | — |
| 2026-10-30 | [AUDIT-2026-10-30-r5-remediation.md](AUDIT-2026-10-30-r5-remediation.md) — R5 remediation record | same file |
| 2026-11-02 | [AUDIT-2026-11-02.md](AUDIT-2026-11-02.md) — wave 2 | — |
| 2026-11-10 | [AUDIT-2026-11-10.md](AUDIT-2026-11-10.md) — 8 P1 / 36 P2 / 47 P3 wave | [REMEDIATION-2026-11-10.md](REMEDIATION-2026-11-10.md) |

The original v1.5.1-era consolidated pair (`AUDIT.md` + `REMEDIATION.md`,
2026-09-28 / 2026-10-03) is archived at
[../history/audits-2026-09/](../history/audits-2026-09/) — it holds the only record
of the committed-token incident response.

Method note: every wave is read-only auditors on disjoint zones, then
direction-matched cross-validation of the fixes; see `CHANGELOG.md` for the
release each wave shipped in.
