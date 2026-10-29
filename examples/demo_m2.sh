#!/usr/bin/env bash
# The M2 demo (task P2-T08; docs/plan/02-roadmap.md, "Milestone demos"): the scenario engine's
# milestone, "Fleet, faults, two clocks", shown end to end on one machine: a fleet of three
# UGVs with namespaces and budgets (P2-T01), a radio link dropped under a driving vehicle
# (P2-T02), a world event and a sensor fault injected from the timeline (P2-T03), the
# real-time-locked clock and two lockstep runs compared (P2-T04), a run matrix with one JUnit
# document (P2-T06), a field log replayed into the stack (P2-T07) and an external vehicle
# repository onboarded through its manifest (P2-T08). Every step is a `fieldproof` command a
# user can run alone; the script runs them in order, keeps going when one fails, and ends with
# a table of verdicts and run directories.
#
# Usage: examples/demo_m2.sh [OPTIONS]
#
#   --only STEPS        run only these steps (comma-separated): fleet, links, events, clocks,
#                       matrix, replay, onboarding
#   --skip STEPS        skip these steps
#   --quick             a workstation short of time: skip the fleet (eight cores) and run two
#                       cases of the matrix instead of twelve (about 20 minutes instead of 90)
#   --local             use the images built locally with `make images` (tag `local`) instead
#                       of the published `main` images from GHCR
#   --no-pull           do not pull the stack images (they are already present)
#   --rebuild-vehicle   rebuild the vehicle images even when they are present
#   --dry-run           print the commands without running them
#   --help              this text
#
# Environment: FIELDPROOF_IMAGE_TAG (default `main`; `--local` sets `local`) selects the stack
# images, FIELDPROOF_RENDER (`auto`, `gpu`, `software`) the simulator's rendering. The radio
# link step needs the `sch_netem` and `ifb` kernel modules (Ubuntu loads them on demand); the
# fleet wants eight cores and 16 GB. Needs Docker and uv on the host (tools/host-setup.sh);
# nothing from ROS or Gazebo installs on the host.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE_PREFIX="${FIELDPROOF_IMAGE_PREFIX:-ghcr.io/dimitrios-kafetzis/fieldproof-}"
STACK_IMAGES=(base-ros2 sim bridge)
VEHICLE_MANIFEST="examples/vehicles/ugv_nav2/robot.yaml"
EXAMPLE_REPO="examples/fieldproof-example-vehicle"
ALL_STEPS=(fleet links events clocks matrix replay onboarding)
FIELDPROOF=(uv run --locked --project engine fieldproof)

only=""
skip=""
quick=false
pull=true
rebuild_vehicle=false
dry_run=false
tag="${FIELDPROOF_IMAGE_TAG:-main}"

usage() {
  sed -n '2,/^set -euo pipefail/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//'
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --only)
      [[ $# -ge 2 ]] || { echo "demo_m2: --only needs a list of steps" >&2; exit 2; }
      only="$2"
      shift 2
      ;;
    --skip)
      [[ $# -ge 2 ]] || { echo "demo_m2: --skip needs a list of steps" >&2; exit 2; }
      skip="$2"
      shift 2
      ;;
    --quick)
      quick=true
      shift
      ;;
    --local)
      tag="local"
      pull=false
      shift
      ;;
    --no-pull)
      pull=false
      shift
      ;;
    --rebuild-vehicle)
      rebuild_vehicle=true
      shift
      ;;
    --dry-run)
      dry_run=true
      shift
      ;;
    --help | -h)
      usage
      exit 0
      ;;
    *)
      echo "demo_m2: unknown option $1 (--help)" >&2
      exit 2
      ;;
  esac
done

known_step() {
  local step
  for step in "${ALL_STEPS[@]}"; do [[ "$step" == "$1" ]] && return 0; done
  return 1
}
for list in "$only" "$skip"; do
  IFS=, read -r -a items <<<"$list"
  for item in "${items[@]}"; do
    [[ -z "$item" ]] || known_step "$item" || { echo "demo_m2: unknown step '$item' (${ALL_STEPS[*]})" >&2; exit 2; }
  done
done
if [[ "$quick" == true ]]; then
  skip="${skip:+$skip,}fleet"
fi

selected() {
  local step="$1"
  if [[ -n "$only" ]] && ! [[ ",$only," == *",$step,"* ]]; then return 1; fi
  if [[ -n "$skip" ]] && [[ ",$skip," == *",$step,"* ]]; then return 1; fi
  return 0
}

step() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
run() {
  echo "+ $*"
  if [[ "$dry_run" == false ]]; then "$@"; fi
}

cd "$REPO"
export FIELDPROOF_IMAGE_TAG="$tag"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
results=()
record() { results+=("$1|$2|$3"); }

# verdict_of RUN_DIR: the report's verdict, or "no report".
verdict_of() {
  if [[ -f "$1/report.json" ]]; then
    python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["verdict"])' "$1/report.json" 2>/dev/null || echo "unreadable"
  else
    echo "no report"
  fi
}

# scenario STEP NAME SCENARIO [RUN ARGS...]: one `fieldproof run`, recorded by its verdict.
scenario() {
  local name="$1" file="$2"
  shift 2
  local run_id="m2-${name}-${stamp}"
  local status=0
  run "${FIELDPROOF[@]}" run "$file" --run-id "$run_id" "$@" || status=$?
  if [[ "$dry_run" == true ]]; then record "$name" "dry run" "runs/$run_id"; return 0; fi
  record "$name" "$(verdict_of "runs/$run_id") (exit $status)" "runs/$run_id"
  return 0
}

step "Prerequisites (docker, uv; see docs/setup/host.md)"
for tool in docker uv; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "demo_m2: $tool is not installed; run tools/host-setup.sh first" >&2
    exit 1
  fi
done
if [[ "$dry_run" == false ]] && ! docker info >/dev/null 2>&1; then
  echo "demo_m2: the Docker daemon is not reachable (is your user in the docker group?)" >&2
  exit 1
fi
images_list="$(IFS=,; echo "${STACK_IMAGES[*]}")"
echo "stack images: ${IMAGE_PREFIX}{${images_list}}:${tag}; steps: ${ALL_STEPS[*]}"
[[ -z "$only" ]] || echo "only: $only"
[[ -z "$skip" ]] || echo "skip: $skip"

if [[ "$pull" == true ]]; then
  step "Pull the published stack images (tag ${tag})"
  for image in "${STACK_IMAGES[@]}"; do
    run docker pull --quiet "${IMAGE_PREFIX}${image}:${tag}"
  done
else
  step "Using the stack images already present (tag ${tag})"
fi

step "Fetch the Gazebo Fuel models pinned in assets/fuel.lock"
run make assets

vehicle_image="$(sed -n 's/^ *image: *//p' "$VEHICLE_MANIFEST" | head -n 1)"
step "The example vehicle image (${vehicle_image})"
if [[ "$rebuild_vehicle" == true ]] || [[ "$dry_run" == true ]] \
  || ! docker image inspect "$vehicle_image" >/dev/null 2>&1; then
  echo "building it from ${VEHICLE_MANIFEST} on ${IMAGE_PREFIX}base-ros2:${tag}"
  run make vehicle-image
else
  echo "already built (--rebuild-vehicle rebuilds it)"
fi

# --- P2-T01: a fleet of three UGVs with namespaces and budgets --------------------------
if selected fleet; then
  step "Fleet: three example UGVs at once, each in its namespace and budget (P2-T01)"
  cores="$(nproc 2>/dev/null || echo 0)"
  if [[ "$cores" -lt 8 && "$dry_run" == false ]]; then
    echo "this host has $cores cores; the fleet wants eight (--quick skips it)"
  fi
  scenario fleet examples/scenarios/ugv_fleet.yaml --check-contracts
fi

# --- P2-T02: a radio link dropped under a driving vehicle -------------------------------
if selected links; then
  step "Radio links: the LTE link to the ground station dropped for 30 s mid-drive (P2-T02)"
  scenario links examples/scenarios/ugv_link_loss.yaml
fi

# --- P2-T03: a world event and a sensor fault from the timeline -------------------------
if selected events; then
  step "Events: a crate dropped in the path, Nav2 replans around it (P2-T03)"
  scenario obstacle examples/scenarios/ugv_obstacle_replan.yaml
  step "Faults: the GNSS fix denied for 20 s under a SLAM stack that never reads it (P2-T03)"
  scenario gps_denied examples/scenarios/ugv_gps_denied.yaml
fi

# --- P2-T04: the real-time-locked clock and two lockstep runs compared ------------------
if selected clocks; then
  step "Two clocks: the drive real-time-locked, the drift measured (P2-T04)"
  scenario realtime examples/scenarios/ugv_realtime.yaml
  step "Two clocks: the drive in lockstep, twice, the trajectories compared (P2-T04)"
  scenario lockstep_a examples/scenarios/ugv_lockstep.yaml
  scenario lockstep_b examples/scenarios/ugv_lockstep.yaml
  status=0
  run "${FIELDPROOF[@]}" compare "runs/m2-lockstep_a-${stamp}" "runs/m2-lockstep_b-${stamp}" || status=$?
  if [[ "$dry_run" == false ]]; then
    record compare "$([[ "$status" -eq 0 ]] && echo "within tolerance" || echo "exit $status")" "-"
  fi
fi

# --- P2-T06: a run matrix, one JUnit document ------------------------------------------
if selected matrix; then
  matrix_flags=()
  if [[ "$quick" == true ]]; then
    step "Run matrix: two cases of the UGV matrix (seeds and budgets), one JUnit document (P2-T06)"
    matrix_flags=(--only 'c01' --only 'c02')
  else
    step "Run matrix: the UGV scenario over six seeds and two budgets, twelve runs, one JUnit document (P2-T06)"
  fi
  status=0
  run "${FIELDPROOF[@]}" matrix examples/matrices/ugv_nav_matrix.yaml "${matrix_flags[@]}" || status=$?
  if [[ "$dry_run" == false ]]; then
    matrix_dir="$(find runs -maxdepth 1 -type d -name 'ugv_nav_matrix-*' -printf '%T@ %p\n' 2>/dev/null | sort -n | tail -n 1 | cut -d' ' -f2-)"
    record matrix "exit $status" "${matrix_dir:-runs/}"
    run "${FIELDPROOF[@]}" runs list -n 15 || true
  fi
fi

# --- P2-T07: a field log replayed into the stack ---------------------------------------
if selected replay; then
  step "Replay: the synthetic field log played into the SLAM stack, its localisation judged (P2-T07)"
  run make replay-log
  scenario replay examples/scenarios/ugv_replay.yaml
fi

# --- P2-T08: an external vehicle repository onboarded through its manifest --------------
if selected onboarding; then
  step "Onboarding: the example vehicle repository, its own stack, built and run through its Makefile (P2-T08)"
  example_image="$(sed -n 's/^ *image: *//p' "$EXAMPLE_REPO/robot.yaml" | head -n 1)"
  if [[ "$rebuild_vehicle" == true ]] || [[ "$dry_run" == true ]] \
    || ! docker image inspect "$example_image" >/dev/null 2>&1; then
    run make -C "$EXAMPLE_REPO" image FIELDPROOF_ROOT="$REPO"
  else
    echo "$example_image already built (--rebuild-vehicle rebuilds it)"
  fi
  run_id="m2-onboarding-${stamp}"
  status=0
  run make -C "$EXAMPLE_REPO" run FIELDPROOF_ROOT="$REPO" RUN_FLAGS="--run-id $run_id" || status=$?
  if [[ "$dry_run" == true ]]; then
    record onboarding "dry run" "$EXAMPLE_REPO/runs/$run_id"
  else
    record onboarding "$(verdict_of "$EXAMPLE_REPO/runs/$run_id") (exit $status)" "$EXAMPLE_REPO/runs/$run_id"
  fi
fi

step "Outcome"
if [[ "$dry_run" == true ]]; then
  echo "Dry run: nothing was executed"
  exit 0
fi
failed=0
printf '%-12s %-28s %s\n' "step" "verdict" "run directory"
for entry in "${results[@]}"; do
  IFS='|' read -r name verdict dir <<<"$entry"
  printf '%-12s %-28s %s\n' "$name" "$verdict" "$dir"
  case "$verdict" in PASS*|"within tolerance"|"exit 0"|"dry run") ;; *) failed=$((failed + 1)) ;; esac
done
echo
echo "Reports: runs/<run_id>/report.md (also report.html, report.json, junit.xml), recordings under recording/,"
echo "the run database: $(printf '%s ' "${FIELDPROOF[@]}")runs list"
if [[ "$failed" -eq 0 ]]; then
  echo "M2 demo: PASS"
else
  echo "M2 demo: $failed step(s) did not pass; see the run directories above" >&2
  exit 1
fi
