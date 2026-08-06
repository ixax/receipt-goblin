# Visuals

A picture only earns its place when it shows something a three-row table cannot: a shape over time, or a share of a whole.
Never send an image on its own - every visual is followed by what it shows, what is abnormal in it, and what that costs.

## Default - draw it from the query results

The report's own charts are built from the numbers this run already pulled, so they can never disagree with the text.
No Grafana, no plugin, no auth, no browser pane.

Draw at most three per run, chosen by what the findings actually are:

| Chart | Source | Use when |
|-------|--------|----------|
| Daily cost with cache-read share overlaid | Q1 + Q2 | Any full run - this is the headline |
| Top operations by cost, horizontal bars | Q4 | A concentration finding: few ops carry the bill |
| Context per call by week, main vs subagent lane | Q11 | A bloat or under-delegation finding |

Render them inline in the chat as a single self-contained SVG or HTML widget.
Keep them readable in both light and dark themes, and label the axes with units the user can act on (dollars, thousands of tokens, share).

For a run that will be re-read later (a scheduled run, or a comparison across weeks), write the same charts plus the report into one HTML file under `.claude/data/usage-coach/` and hand it over as a file, rather than only inline.

## Grafana panel screenshots

Use these when the user asks for the actual panel, or when a finding points at a specific panel they will want to open anyway.

Anonymous viewer access is enabled on this stack, so no login step is needed.
Panel ids come from `parse_dashboard.py list-panels` - never guessed.

Single-panel URL shape:

> http://localhost:3000/d/agents-overview/agents-overview?viewPanel=panel-ID&from=now-30d&to=now

Two ways to capture it, in order of preference:

1. In-app browser - navigate to that URL and screenshot.
   Requires the browser pane to be visible on the user's screen; a hidden pane composites no frames and the screenshot times out.
   If it times out, ask the user to open the pane rather than retrying blindly.
2. Grafana's own render endpoint, saved to a file under `.claude/data/usage-coach/` and handed over.
   Served by the `grafana-renderer` sidecar (`agent_docs/services/grafana.md`), so it works headlessly, with no browser pane and no auth argument of its own.

   > curl -o panel.png "http://localhost:3000/render/d-solo/agents-overview/agents-overview?panelId=panel-31&width=1000&height=500&from=now-30d&to=now"

   `panelId` takes the element name from `list-panels` (`panel-31`), not the bare number - this dashboard is on the v2 schema.
   A 500 means the sidecar is down; a 302 to a login page means anonymous access was turned off.
   Report either as the cause instead of retrying.

Prefer option 2 whenever it answers; fall back to option 1 only while the sidecar is down.

## Explaining a visual

Three sentences, in this order, every time:

> What the picture plots, in the user's own terms.
> The one thing in it that is off - the spike, the divergence, the flat line that should be sloping.
> What that costs over the window, and which finding it belongs to.

An image the user has to interpret alone is a failed report.
