# Git: ask before destructive actions

Full rule content for `AGENTS.md`'s "Git: ask before destructive actions" pointer.
See `agent_docs/incidents.md` for the incident history behind this rule, not duplicated here.

## The core rule

Never run `git checkout --`/`restore`/`reset --hard`/`clean` (or any other command that discards uncommitted working-tree changes) without asking first, even to undo your own bad edit on a file that looks like it only contains your own changes.
`git status` can't tell you whose changes are sitting there or how far back they go; a file already modified before you touched it is a signal that discarding it isn't yours to decide.
Recovery is normally impossible: a path never `git add`ed leaves no blob to recover from `git fsck`/reflog.
If a change you made needs undoing, edit it back by hand instead of reverting the whole file to `HEAD`, which also wipes any pre-existing uncommitted work in the same file.

## Binds every delegated subagent too

Having Bash/git access doesn't make a subagent exempt.
If a file's state looks wrong mid-task, stop and report the anomaly to the caller instead of self-recovering with git.
Diagnose by reading/grepping the file's actual current content, never by diffing against a git baseline that's likely stale relative to real uncommitted work already there.

## The `git show :path` variant and the broader atomic-cycle rule

The same failure has a variant with none of the four named commands.
`git show :path` (or any other command writing a stored ref's content over the live working-tree file) is functionally identical to `git checkout -- path`, even though it isn't literally `checkout`/`restore`/`reset`/`clean`.
The underlying rule is broader than those four commands: never hold a file's full content in memory across more than one edit, and never write a file's full content back unless every byte was just read fresh, immediately before writing.
A large multi-edit task (5 edits or 500) does read-edit-write as one atomic cycle per edit, in a loop, not read-once/edit-many-in-memory/write-once.

## Hook-enforcement status

`git checkout --`/`git restore` are hook-enforced (`hooks/guard_destructive.py` forces a confirmation prompt), not prose-only anymore.
The `git show :path` variant and the broader "never hold a file's full content across edits" rule remain not hook-covered: no pattern catches those, prose discipline is all that applies.
