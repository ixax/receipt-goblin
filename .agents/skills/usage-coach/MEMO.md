# usage-coach memo

State carried between `/usage-coach` runs.
Written by the skill, read by the skill before every analysis.
Hand edits are fine - the user's verdict beats the skill's.

Keep the last 12 run entries; prune older ones when appending.
One sentence per line, same as every other markdown file in this repo.

## Dashboard fingerprint

From `parse_dashboard.py summary` and `list-panels`, compared at the start of every run.
Panel ids are the identity; titles are only for reading.

| Field | Value |
|-------|-------|
| Tabs | (unrecorded) |
| Panels | (unrecorded) |
| Panel id-to-title list | Not stored inline - re-derive with `list-panels` and compare counts first; store the full list only once a drift needs pinpointing |

## Health baselines

Last observed value per check, so a slow drift is visible instead of only a threshold crossing.

| Check | Last value | Last fired |
|-------|------------|------------|
| _none yet_ | | |

## Revised thresholds

Overrides `references/playbook.md`.
Each row carries the evidence that moved it.

| Signal | Playbook value | Revised value | Evidence |
|--------|----------------|---------------|----------|
| _none yet_ | | | |

## Candidate signals

Waste patterns seen here that the playbook has no signal for.
A candidate that fires usefully twice gets proposed for promotion into the playbook.

| Pattern | Query needed | Times fired usefully |
|---------|--------------|----------------------|
| _none yet_ | | |

## Closed recommendations

Recommendations the user rejected, and why - never propose these again.

| Recommendation | Reason closed |
|----------------|---------------|
| _none yet_ | |

## Runs

Newest first.
Each entry uses this shape:

> ### run YYYY-MM-DD, scope `<arg>`
>
> Baseline: cost `$X`, cache_read_share `Y`, main-lane share `Z`, p50/p90 session `$A`/`$B`, failure share `C`.
> Health: checks that fired, or `clean`.
> Dashboard: `unchanged`, or what drifted.
> Verdicts: one line per previously open recommendation, with its grade.
> Open: one line per recommendation left open, each with its target metric and direction.

_(no runs recorded yet)_
