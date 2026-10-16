#!/bin/bash
# cspell:ignore piomar rerere toplevel worktrees
# -----------------------------------------------------------------------------
# Rebuild the fork's `patched` branch: upstream + every branch in branches.txt
# + this fork-glue branch + one commit with local kernel numbering and binaries.
#
# Usage (from any checkout of the fork, with remotes `origin` = upstream and
# `fork` = piomar123/batpred):
#   tools/fork/rebuild.sh [--base REF] [--no-test] [--push] [--tag NAME]
#
#   --base REF  what to build on: origin/main (default) or an upstream tag.
#               A tag only works when every branch is based at or before it.
#   --no-test   skip the unit tests and the kernel parity check
#   --push      force-push the result to fork/patched (with lease)
#   --tag NAME  tag the result (annotated) and push the tag with --push
#
# The build happens in the worktree .worktrees/patched (created if missing).
# Conflicts between branches are replayed from git rerere; a conflict rerere
# has not seen stops the script - resolve it in .worktrees/patched, commit,
# and run the script again (rerere then remembers the resolution).
# Kernel binaries are never merged: the script takes either side and rebuilds
# all six with zig (ZIG=/path/to/zig, version pinned in code-quality.yml).
# -----------------------------------------------------------------------------
set -euo pipefail

BASE="origin/main"
RUN_TESTS=1
PUSH=0
TAG=""
while [ $# -gt 0 ]; do
    case "$1" in
        --base) BASE="$2"; shift 2 ;;
        --no-test) RUN_TESTS=0; shift ;;
        --push) PUSH=1; shift ;;
        --tag) TAG="$2"; shift 2 ;;
        *) echo "Unknown option $1"; exit 1 ;;
    esac
done

REPO="$(git rev-parse --show-toplevel)"
COMMON="$(cd "$REPO" && cd "$(git rev-parse --git-common-dir)" && pwd)"
MAIN_CHECKOUT="$(dirname "$COMMON")"
WT="$MAIN_CHECKOUT/.worktrees/patched"
ZIG="${ZIG:-zig}"
GLUE="fork-glue"
LOCAL_KERNEL_OFFSET=1000

command -v "$ZIG" >/dev/null 2>&1 || { echo "ERROR: zig not found, set ZIG=/path/to/zig"; exit 1; }

git -C "$REPO" fetch -q origin --tags
git -C "$REPO" fetch -q --prune fork

# The branch list is read from the fork's published glue branch, so a rebuild never
# depends on what a local checkout happens to have
BRANCHES=$(git -C "$REPO" show "fork/$GLUE:tools/fork/branches.txt" | sed -e 's/#.*//' | awk 'NF {print $1}')

BASE_SHA=$(git -C "$REPO" rev-parse --verify "$BASE^{commit}")
for b in $BRANCHES $GLUE; do
    git -C "$REPO" rev-parse --verify -q "fork/$b" >/dev/null || { echo "ERROR: fork/$b does not exist"; exit 1; }
    # A branch based after BASE would drag the upstream commits between the two into the build
    fork_point=$(git -C "$REPO" merge-base "fork/$b" origin/main)
    if ! git -C "$REPO" merge-base --is-ancestor "$fork_point" "$BASE_SHA"; then
        echo "ERROR: fork/$b is based on $(git -C "$REPO" describe --tags "$fork_point"), after $BASE - rebuild on a later base or rebase the branch"
        exit 1
    fi
done

if [ ! -d "$WT" ]; then
    git -C "$MAIN_CHECKOUT" worktree add -q --detach "$WT" "$BASE_SHA"
fi
cd "$WT"
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    echo "ERROR: $WT has uncommitted changes"
    exit 1
fi
git config rerere.enabled true
git config rerere.autoupdate true
git checkout -q -B patched "$BASE_SHA"

merge_one() {
    local ref="$1"
    echo "Merging $ref"
    if git merge -q --no-ff --no-edit "$ref"; then
        return 0
    fi
    # Binaries are rebuilt below, so either side will do
    { git diff --name-only --diff-filter=U | grep '\.so$' || true; } | while read -r so; do
        git checkout -q --ours -- "$so"
        git add "$so"
    done
    if [ -n "$(git diff --name-only --diff-filter=U)" ] || git diff --cached | grep -q '^+<<<<<<< '; then
        echo "ERROR: merging $ref left conflicts rerere doesn't know:"
        git diff --name-only --diff-filter=U
        echo "Resolve them in $WT, commit, then run this script again"
        exit 1
    fi
    git commit -q --no-edit
}

for b in $BRANCHES; do
    merge_one "fork/$b"
done
merge_one "fork/$GLUE"

# Local kernel numbering: well away from upstream's, so a stale upstream binary is always
# rejected and never loaded against this combination of PkContext fields
K_PY=apps/predbat/prediction_kernel.py
K_CPP=apps/predbat/prediction_kernel.cpp
abi=$(sed -n 's/^KERNEL_ABI_VERSION = \([0-9]*\)$/\1/p' "$K_PY")
parity=$(sed -n 's/^KERNEL_PARITY_REVISION = \([0-9]*\)$/\1/p' "$K_PY")
[ -n "$abi" ] && [ -n "$parity" ] || { echo "ERROR: kernel version not found in $K_PY"; exit 1; }
new_abi=$((abi + LOCAL_KERNEL_OFFSET))
new_parity=$((parity + LOCAL_KERNEL_OFFSET))
perl -pi -e "s/^KERNEL_ABI_VERSION = $abi\$/KERNEL_ABI_VERSION = $new_abi/; s/^KERNEL_PARITY_REVISION = $parity\$/KERNEL_PARITY_REVISION = $new_parity/" "$K_PY"
perl -pi -e "s|^#define PK_ABI_VERSION $abi\$|// Fork (tools/fork/rebuild.sh): local ABI $new_abi = $LOCAL_KERNEL_OFFSET + $abi for this combination of branches.\n#define PK_ABI_VERSION $new_abi|; s|^#define PK_PARITY_REVISION $parity\$|// Fork (tools/fork/rebuild.sh): local parity $new_parity = $LOCAL_KERNEL_OFFSET + $parity.\n#define PK_PARITY_REVISION $new_parity|" "$K_CPP"
grep -q "^KERNEL_ABI_VERSION = $new_abi\$" "$K_PY" && grep -q "^KERNEL_PARITY_REVISION = $new_parity\$" "$K_PY" || { echo "ERROR: kernel version not updated in $K_PY"; exit 1; }
grep -q "^#define PK_ABI_VERSION $new_abi\$" "$K_CPP" && grep -q "^#define PK_PARITY_REVISION $new_parity\$" "$K_CPP" || { echo "ERROR: kernel version not found in $K_CPP"; exit 1; }
ZIG="$ZIG" bash apps/predbat/build_kernel_cross.sh >/dev/null
git add "$K_PY" "$K_CPP" apps/predbat/prediction_kernel_lib_*.so
git commit -q -m "fork: local kernel ABI $new_abi / parity $new_parity and rebuilt binaries

Generated by tools/fork/rebuild.sh on $(git describe --tags "$BASE_SHA")."

if [ "$RUN_TESTS" = 1 ]; then
    [ -x coverage/venv/bin/python ] || { echo "ERROR: no coverage/venv in $WT (see coverage/setup.csh)"; exit 1; }
    LOG="$WT/coverage/rebuild-tests.log"
    echo "Running unit tests, log in $LOG"
    (cd coverage && ./venv/bin/python ../apps/predbat/unit_test.py >"$LOG" 2>&1) || { echo "ERROR: unit tests failed, see $LOG"; exit 1; }
    case "$(uname -s)-$(uname -m)" in
        Darwin-arm64) so=prediction_kernel_lib_darwin_arm64.so ;;
        Darwin-x86_64) so=prediction_kernel_lib_darwin_x86_64.so ;;
        Linux-*) so="prediction_kernel_lib_$(uname -m).so" ;;
    esac
    (cd coverage && ./venv/bin/python ../apps/predbat/verify_kernel_binary.py "../apps/predbat/$so" >>"$LOG" 2>&1) || { echo "ERROR: kernel parity failed for $so, see $LOG"; exit 1; }
    echo "Tests passed: $(grep -c 'PASSED in' "$LOG") test entries, kernel $so parity passed"
fi

if [ -n "$TAG" ]; then
    git tag -a "$TAG" -m "Patched build on $(git describe --tags "$BASE_SHA")

Branches: $(echo $BRANCHES | tr ' ' ',')
Kernel ABI $new_abi / parity $new_parity"
fi
if [ "$PUSH" = 1 ]; then
    git push -q --force-with-lease fork patched
    [ -z "$TAG" ] || git push -q fork "refs/tags/$TAG"
fi
echo "Built patched at $(git rev-parse --short HEAD) on $(git describe --tags "$BASE_SHA")"
git log --oneline --first-parent "$BASE_SHA"..HEAD
