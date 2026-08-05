#!/bin/sh
# Regenerates agent_docs/ast_index/ entries for every .py file changed in
# this push, then blocks if that leaves anything uncommitted.
# Deliberately does not auto-commit - matches check-lock.sh's fail-and-instruct style.
#
# Reads pushed refs from stdin per git's pre-push protocol:
#   <local ref> <local sha> <remote ref> <remote sha>

ZERO_SHA=0000000000000000000000000000000000000000
EMPTY_TREE=4b825dc642cb6eb9a060e54bf8d69288fbee4904

changed_files=""
while read -r local_ref local_sha remote_ref remote_sha; do
    [ "$local_sha" = "$ZERO_SHA" ] && continue  # branch deletion, nothing to check
    if [ "$remote_sha" = "$ZERO_SHA" ]; then
        base="$EMPTY_TREE"
    else
        base="$remote_sha"
    fi
    changed_files="$changed_files
$(git diff --name-only "$base" "$local_sha" -- '*.py')"
done

changed_files=$(printf '%s\n' "$changed_files" | sort -u | grep -v '^$')

if [ -z "$changed_files" ]; then
    exit 0
fi

for path in $changed_files; do
    [ -f "$path" ] || continue
    uv run python3 scripts/ast_index.py build --file "$path" || exit 1
done

if [ -n "$(git status --porcelain -- agent_docs/ast_index/)" ]; then
    echo "error: agent_docs/ast_index/ is out of date for this push:"
    git status --porcelain -- agent_docs/ast_index/
    echo "review, commit, and push again:"
    echo "  git add agent_docs/ast_index/ && git commit -m 'ast-index: refresh' && git push"
    exit 1
fi

exit 0
