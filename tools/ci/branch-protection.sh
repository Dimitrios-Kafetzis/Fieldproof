#!/usr/bin/env bash
# Wire the CI status checks into the branch protection of main (task P0-T05).
#
# Usage: tools/ci/branch-protection.sh [--check] [--repo OWNER/NAME]
#
# Without --check, sets the required status checks of `main` to the contexts listed in
# REQUIRED_CHECKS (the `name:` of the gate jobs in .github/workflows) reported by the GitHub
# Actions app, keeps "require branches to be up to date" on, and turns on the repository setting
# that requires actions to be pinned to a commit SHA. With --check it only prints the current
# state and exits 1 when it differs. Needs `gh` authenticated as a repository admin.
set -euo pipefail

REQUIRED_CHECKS=("CI" "PR title")
GITHUB_ACTIONS_APP_ID=15368
BRANCH=main

repo=""
check=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --check) check=1; shift ;;
    --repo)
      [[ $# -ge 2 ]] || { echo "branch-protection: --repo needs OWNER/NAME" >&2; exit 2; }
      repo="$2"; shift 2 ;;
    -h | --help)
      sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) echo "branch-protection: unknown argument '$1'" >&2; exit 2 ;;
  esac
done
if [[ -z "$repo" ]]; then
  repo="$(gh repo view --json nameWithOwner --jq .nameWithOwner)"
fi

want="$(printf '%s\n' "${REQUIRED_CHECKS[@]}" | sort)"
have="$(gh api "repos/${repo}/branches/${BRANCH}/protection/required_status_checks" \
  --jq '.checks[].context' | sort)"
strict="$(gh api "repos/${repo}/branches/${BRANCH}/protection/required_status_checks" --jq .strict)"
pinning="$(gh api "repos/${repo}/actions/permissions" --jq '.sha_pinning_required // false')"

echo "repository:              $repo"
echo "required checks (now):   $(tr '\n' ',' <<<"$have" | sed 's/,$//; s/,/, /g')"
echo "required checks (want):  $(tr '\n' ',' <<<"$want" | sed 's/,$//; s/,/, /g')"
echo "up to date before merge: $strict"
echo "actions SHA pinning:     $pinning"

if [[ "$check" -eq 1 ]]; then
  if [[ "$have" == "$want" && "$strict" == "true" && "$pinning" == "true" ]]; then
    echo "branch protection matches"
    exit 0
  fi
  echo "branch protection differs" >&2
  exit 1
fi

checks_json="$(printf '%s\n' "${REQUIRED_CHECKS[@]}" \
  | jq -R --argjson app "$GITHUB_ACTIONS_APP_ID" '{context: ., app_id: $app}' | jq -s .)"
jq -n --argjson checks "$checks_json" '{strict: true, checks: $checks}' \
  | gh api -X PATCH "repos/${repo}/branches/${BRANCH}/protection/required_status_checks" \
    --input - --jq '"required checks set: " + ([.checks[].context] | join(", "))'

# The permissions endpoint replaces the whole object, so the current values are carried over.
gh api "repos/${repo}/actions/permissions" \
  | jq '{enabled, allowed_actions, sha_pinning_required: true}' \
  | gh api -X PUT "repos/${repo}/actions/permissions" --input - >/dev/null \
  && echo "actions SHA pinning required: true"
