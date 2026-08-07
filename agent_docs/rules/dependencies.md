# Service dependency edits

Full rule content for `AGENTS.md`'s "service dependency edits" pointer.

A dependency change is a three-step chain - stopping early leaves the change with no effect anywhere.

1. Edit `services/<svc>/requirements.txt` - direct deps only, keep the `why` comments.
2. Run `make lock` - regenerates `requirements.lock` (full transitive pin).
   Images install from the `.lock`, never the `.txt`, so an unlocked edit reaches nothing.
   Commit both files together - `.githooks/lib/check-lock.sh` fails the commit otherwise.
3. Hand the rebuild to `dev-ops` - a lock change is a baked-in-file change like any other, so it only reaches a running container via its rebuild.
   Never rebuild inline instead.
