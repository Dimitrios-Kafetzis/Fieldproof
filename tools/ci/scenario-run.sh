#!/usr/bin/env bash
# Run a scenario with `fieldproof run` under the flaky-run policy of task P1-T07: one retry,
# flagged. The scenario workflow (.github/workflows/scenario-tests.yml) calls this script; it
# also works on a workstation.
#
# Usage: tools/ci/scenario-run.sh [OPTIONS] SCENARIO [-- FIELDPROOF RUN ARGS...]
#
#   --runs-dir DIR    where the attempts' run directories go (default: runs)
#   --run-id ID       base of the attempts' run ids (default: scenario-<name>); attempt N runs
#                     as <ID>-attempt<N>
#   --retries N       how many times a failed attempt is retried (default: 1; 0 disables)
#   --render MODE     rendering of the simulator: auto, gpu or software (default: software)
#   --summary FILE    append the Markdown summary to FILE (default: $GITHUB_STEP_SUMMARY when
#                     set, nothing otherwise)
#   --help            this text
#
# An attempt that exits 0 ends the loop. An attempt that exits 130 (interrupted) or 2 (usage
# error) is never retried: neither is a flaky run. Any other exit code is retried once a
# warning has been printed (a GitHub Actions `::warning::` annotation on a runner), so a run
# that passes on its second attempt is green but flagged: the summary says so, the step output
# `flaky` is `true`, and both attempts' run directories stay in --runs-dir for the artefacts.
# The outcome of the whole is the last attempt's: its exit code is the script's, its
# `junit.xml` and `report.md` are copied to the top of --runs-dir, and `run_id`, `run_dir`,
# `verdict`, `exit_code`, `attempts` and `flaky` are written to $GITHUB_OUTPUT when set.
#
# The command comes from FIELDPROOF (default: `uv run --locked --project engine fieldproof`,
# split on spaces), so the tests substitute a stub.
set -euo pipefail

runs_dir="runs"
run_id=""
retries=1
render="software"
summary="${GITHUB_STEP_SUMMARY:-}"
scenario=""
extra=()

usage() {
  sed -n '2,/^set -euo pipefail/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//'
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --runs-dir)
      [[ $# -ge 2 ]] || { echo "scenario-run: --runs-dir needs a directory" >&2; exit 2; }
      runs_dir="$2"
      shift 2
      ;;
    --run-id)
      [[ $# -ge 2 ]] || { echo "scenario-run: --run-id needs an id" >&2; exit 2; }
      run_id="$2"
      shift 2
      ;;
    --retries)
      [[ $# -ge 2 && "$2" =~ ^[0-9]+$ ]] || { echo "scenario-run: --retries needs a number" >&2; exit 2; }
      retries="$2"
      shift 2
      ;;
    --render)
      [[ $# -ge 2 ]] || { echo "scenario-run: --render needs a mode" >&2; exit 2; }
      render="$2"
      shift 2
      ;;
    --summary)
      [[ $# -ge 2 ]] || { echo "scenario-run: --summary needs a file" >&2; exit 2; }
      summary="$2"
      shift 2
      ;;
    --help | -h)
      usage
      exit 0
      ;;
    --)
      shift
      extra=("$@")
      break
      ;;
    -*)
      echo "scenario-run: unknown option $1" >&2
      exit 2
      ;;
    *)
      if [[ -n "$scenario" ]]; then
        echo "scenario-run: one scenario at a time (got '$scenario' and '$1')" >&2
        exit 2
      fi
      scenario="$1"
      shift
      ;;
  esac
done
[[ -n "$scenario" ]] || { echo "scenario-run: a scenario file is required (--help)" >&2; exit 2; }
[[ -f "$scenario" ]] || { echo "scenario-run: $scenario: no such file" >&2; exit 2; }

name="$(basename "${scenario%.*}")"
[[ -n "$run_id" ]] || run_id="scenario-${name}"
read -r -a fieldproof <<<"${FIELDPROOF:-uv run --locked --project engine fieldproof}"
mkdir -p "$runs_dir"

attempts=$((retries + 1))
attempt=0
status=1
last_id=""
verdicts=()
while [[ "$attempt" -lt "$attempts" ]]; do
  attempt=$((attempt + 1))
  last_id="${run_id}-attempt${attempt}"
  echo "scenario-run: attempt ${attempt} of ${attempts}: ${fieldproof[*]} run ${scenario} --run-id ${last_id}"
  status=0
  "${fieldproof[@]}" run "$scenario" --runs-dir "$runs_dir" --run-id "$last_id" --render "$render" \
    "${extra[@]}" || status=$?
  verdict="exit ${status}"
  if [[ -f "$runs_dir/$last_id/report.json" ]]; then
    verdict="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["verdict"])' \
      "$runs_dir/$last_id/report.json" 2>/dev/null || echo "$verdict")"
  fi
  verdicts+=("attempt ${attempt}: ${verdict} (${last_id})")
  echo "scenario-run: attempt ${attempt} ended with exit code ${status} (${verdict})"
  if [[ "$status" -eq 0 || "$status" -eq 130 || "$status" -eq 2 ]]; then
    break
  fi
  if [[ "$attempt" -lt "$attempts" ]]; then
    echo "::warning title=Scenario ${name} retried::attempt ${attempt} ended with exit code ${status} (${verdict}); retrying once"
  fi
done

flaky=false
if [[ "$status" -eq 0 && "$attempt" -gt 1 ]]; then
  flaky=true
  echo "::warning title=Flaky scenario ${name}::passed on attempt ${attempt} of ${attempts}; see the earlier attempt's run directory"
elif [[ "$status" -ne 0 ]]; then
  echo "::error title=Scenario ${name} ${verdict}::exit code ${status} after ${attempt} attempt(s); run directory ${runs_dir}/${last_id}"
fi

run_dir="$runs_dir/$last_id"
for file in junit.xml report.md report.json; do
  if [[ -f "$run_dir/$file" ]]; then
    cp "$run_dir/$file" "$runs_dir/$file"
  fi
done

if [[ -n "$summary" ]]; then
  {
    echo "## Scenario \`${name}\`: ${verdict}"
    echo
    if [[ "$flaky" == true ]]; then
      echo "> **Flaky:** passed on attempt ${attempt} of ${attempts}. Every attempt's run directory is in the artefacts."
      echo
    fi
    for line in "${verdicts[@]}"; do
      echo "- ${line}"
    done
    echo
    if [[ -f "$run_dir/report.md" ]]; then
      # The report's own title becomes a third-level heading under this one.
      sed 's/^# /### /' "$run_dir/report.md"
    else
      echo "No report was written (the run did not reach its end)."
    fi
  } >>"$summary"
fi

if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
  {
    echo "run_id=${last_id}"
    echo "run_dir=${run_dir}"
    echo "verdict=${verdict}"
    echo "exit_code=${status}"
    echo "attempts=${attempt}"
    echo "flaky=${flaky}"
  } >>"$GITHUB_OUTPUT"
fi

exit "$status"
