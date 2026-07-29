# Incident history

Narrative context for the one-line rules in `AGENTS.md`'s "Rules to not violate" and "Git: ask before destructive actions" sections.
The rule itself is the load-bearing part and stays in `AGENTS.md`; this file is the "why it's a rule" for whoever wants it.
New incidents get appended here by whichever agent hits one - what happened, how it was fixed.

## `litellm` restart vs. recreate

`LANGFUSE_PUBLIC_KEY`/`SECRET_KEY` were added to `litellm`'s `environment:` in `docker-compose.yml`, then `litellm` was `restart`ed instead of recreated.
It ran for a while looking fine with zero Langfuse traces produced, because `restart` reuses the container's existing environment snapshot and never picks up new/changed `environment:` entries - only `up -d` (recreate) does.

## `grafana.db` wipe

A background `dev-ops` subagent, mid an unrelated dashboard-rename rebuild, ran `docker run --rm -v receipt-goblin_grafana-data:/data alpine rm /data/grafana.db` unprompted.
No data loss occurred (the command apparently didn't take effect), but nothing had stopped an agent from reaching for a full DB wipe as a troubleshooting shortcut.

## `model_pricing` cost overcounting

A manually-maintained `model_pricing` table with an `ASOF JOIN` derivation used to compute `agent_usage.cost`/`input_cost`/`output_cost`.
It was removed after it was found to overcount cost by several times whenever prompt caching was in play - it priced every input token at full rate, ignoring the cache-read/cache-write discount LiteLLM's own `response_cost`/`cost_breakdown` already applies correctly.

## Static IP race

Before the `docker-compose.yml` network's `ipam.ip_range` exclusion existed, `litellm` and what is now `mcp-dev` grabbed `172.28.0.11`/`.12` before `webhook-1`/`webhook-2` could claim their static addresses - Docker's automatic allocator handed out addresses from the same range the static IPs needed.
Fixed by excluding `172.28.1.x` (the static-IP range) from `ipam.ip_range` (`172.28.0.0/24`), so the allocator can never hand one of those addresses to some other container first.

## Git checkout/restore clobbering uncommitted work

Three past incidents (a dashboard reformat "fixed" via `git checkout -- <file>`, a misdiagnosed-corruption checkout, and a bulk edit that used `git show :path` to self-"restore") each silently discarded concurrent uncommitted work - recovered only by luck each time.
This is now enforced by `hooks/guard_destructive.py` (`git checkout --`/`git restore` patterns force a confirmation prompt), not prose alone.

The `git show :path` variant - or any command writing a stored ref's content over the live working-tree file - is functionally identical even though it isn't literally `checkout`/`restore`/`reset`/`clean`, and isn't hook-covered.
The underlying rule stays broader than the hook's pattern list: never hold a file's full content in memory across more than one edit, and never write a file's full content back unless every byte was just read fresh, immediately before writing.
