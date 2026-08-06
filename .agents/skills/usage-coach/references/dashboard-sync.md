# Dashboard sync and coverage advice

Keeps the skill honest about a dashboard that changes under it, and turns the comparison into advice about what the observability setup itself is missing.

Read `services/grafana/dashboards/agents_overview.json` only through `services/grafana/scripts/parse_dashboard.py` - the file is large and hand-parsing it inline is banned.
Delegate the reads to `dashboard-parser` when a run needs more than the two commands below.

## Fingerprint

Two commands produce everything the sync needs:

```bash
python3 services/grafana/scripts/parse_dashboard.py summary services/grafana/dashboards/agents_overview.json
python3 services/grafana/scripts/parse_dashboard.py list-panels services/grafana/dashboards/agents_overview.json
```

Store in `MEMO.md`: tab count, panel count, and the panel id-to-title list.
Compare against the stored fingerprint at the start of every run.

## Reacting to drift

| Drift | What it breaks | Action |
|-------|----------------|--------|
| Panel added | Nothing, but the skill may now be duplicating it | Map it to a metric in the pack; if it covers a signal the pack lacks, propose adopting it |
| Panel removed | Any screenshot target and any prior finding that cited it | Drop the reference, and note in the memo that findings citing it are no longer verifiable |
| Panel retitled | Only text references, since ids are stable | Re-map by id, update the stored title |
| Panel query changed | The baseline taken from it is no longer comparable | Mark the affected baseline `incomparable` in the memo rather than reporting a fake trend |
| Tab restructured | Panel ids survive, screenshot URLs survive | Update the fingerprint, no other action |

A panel id is the stable identity - never key anything to a title.

## Coverage advice

After syncing, compare in both directions and report anything worth acting on.

Signals in `playbook.md` with no panel behind them are candidates for a new panel, since a signal that only exists inside this skill is invisible between runs.
Propose the panel to `dashboards-expert` with its query and the tab it belongs in - never edit dashboard JSON from this skill.

Panels showing something the metric pack ignores are candidates for adoption into `queries.md`, especially anything the user says they check by hand.

Rank both lists by how much money the missing view could have caught, and propose at most two per run.

## Missing statistics

Some advice is impossible because the data is not collected at all, not because a query is missing.
When a run hits that wall, say so explicitly and name what would have to be captured.

Check `plans/features/` before proposing anything here - several gaps already have approved designs, and duplicating one wastes the user's time.
A genuinely new gap goes through `Skill(clickhouse-migration)` for the schema change, and never through an ad-hoc column.

The bar for proposing new collection is evidence: name the finding that could not be made this run without it.

## Improving the skill itself

Every run ends with one honest look at this skill's own performance, recorded in `MEMO.md`:

- A query in the pack that produced nothing usable in three consecutive runs is dead weight - propose deleting it.
- A finding the user acted on that came from manual digging rather than the pack means the pack has a hole - propose the query.
- A recommendation graded `not applied` twice is written wrong, not ignored - propose a smaller version of it.
- A threshold that fired on noise twice gets loosened in the memo immediately, and only reaches `playbook.md` with the user's approval and a version bump.

Propose at most one skill change per run, with the evidence attached.
Self-improvement that is not grounded in a graded outcome is just churn.
