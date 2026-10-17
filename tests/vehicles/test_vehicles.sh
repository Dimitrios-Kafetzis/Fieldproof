#!/usr/bin/env bash
# Tests for the vehicle manifests and images (task P1-T04): the manifest schema, the Dockerfile
# generator and the example vehicle ugv_nav2.
#
# 1. Static checks: bash -n and shellcheck on this script; every robot.yaml in the repository
#    validates against schema v0 (`fieldproof manifest validate`) and against the committed JSON
#    Schema (check-jsonschema, as the pre-commit hook does); the committed schema is current.
# 2. Generator: the build context of the example vehicle is written; its Dockerfile passes hadolint
#    with the repository's configuration and its entry point passes shellcheck; the resolved
#    manifest has no `extends` and keeps the reference model's interfaces.
# 3. Image: `fieldproof vehicle build` builds the example image on the local base image (minutes:
#    Nav2 and SLAM Toolbox from the apt snapshots), or with --no-build the image already present
#    is used; --static skips this section. The image runs as the non-root user, carries the
#    manifest, the install space, the readiness probe (its HEALTHCHECK) and the labels, has no
#    compiler, finds the bringup package and its parameter files, and shows the field launch
#    arguments; the probe alone reports every condition pending.
# 4. Graph: the image started with `fieldproof vehicle run` on the `fieldproof` network joins the
#    ROS graph: from a second container of the same image on the same network, its nodes are
#    listed, SLAM Toolbox is `active` and Nav2's servers answer their lifecycle services (they
#    stay `inactive` until the bridge publishes TF, which tests/integration adds); `fieldproof
#    vehicle wait` and the health check report Nav2 pending; no node dies; the container carries
#    the manifest's budget. --static and --no-graph skip it.
#
# Usage: tests/vehicles/test_vehicles.sh [--no-build] [--no-graph] [--static] [--help]
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SHELLCHECK_IMAGE="koalaman/shellcheck:v0.10.0@sha256:2097951f02e735b613f4a34de20c40f937a6c8f18ecb170612c88c34517221fb"
HADOLINT_IMAGE="hadolint/hadolint:v2.12.0@sha256:30a8fd2e785ab6176eed53f74769e04f125afb2f74a6c52aef7d463583b6d45e"
MANIFEST="${VEHICLE_MANIFEST:-examples/vehicles/ugv_nav2/robot.yaml}"
BASE_IMAGE="${VEHICLE_BASE_IMAGE:-ghcr.io/dimitrios-kafetzis/fieldproof-base-ros2:local}"
NETWORK="${VEHICLE_NETWORK:-fieldproof}"
FIELDPROOF=(uv run --locked --project "$REPO/engine" fieldproof)
SCRATCH="$(mktemp -d)"
CONTAINER="fieldproof-vehicle-test-$$"
cleanup() {
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  rm -rf "$SCRATCH"
}
trap cleanup EXIT

NO_BUILD=0
NO_GRAPH=0
STATIC=0
for arg in "$@"; do
  case "$arg" in
    --no-build) NO_BUILD=1 ;;
    --no-graph) NO_GRAPH=1 ;;
    --static) STATIC=1 ;;
    -h | --help)
      sed -n '2,23p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "unknown argument '$arg' (expected --no-build, --no-graph, --static or --help)" >&2
      exit 2
      ;;
  esac
done

PASSED=0
FAILED=0
pass() {
  printf '  \033[1;32mok\033[0m    %s\n' "$1"
  PASSED=$((PASSED + 1))
}
fail() {
  printf '  \033[1;31mFAIL\033[0m  %s\n' "$1"
  FAILED=$((FAILED + 1))
}
skip() { printf '  \033[1;33mskip\033[0m  %s\n' "$1"; }
section() { printf '\n\033[1m%s\033[0m\n' "$1"; }

cd "$REPO"
uv sync --locked --project engine --quiet

# ---------------------------------------------------------------------------------------------
section "1. Static checks"
if bash -n tests/vehicles/test_vehicles.sh; then pass "bash -n tests/vehicles/test_vehicles.sh"; else fail "bash -n"; fi
if command -v shellcheck >/dev/null 2>&1; then
  if shellcheck --severity=style --shell=bash tests/vehicles/test_vehicles.sh; then pass "shellcheck (local)"; else fail "shellcheck (local)"; fi
elif docker run --rm -v "${REPO}:/mnt:ro" "$SHELLCHECK_IMAGE" --severity=style --shell=bash tests/vehicles/test_vehicles.sh; then
  pass "shellcheck (container ${SHELLCHECK_IMAGE%%@*})"
else
  fail "shellcheck (container)"
fi
mapfile -t manifests < <(git ls-files '*robot.yaml' 'robot.yaml')
if [[ ${#manifests[@]} -ge 2 ]]; then pass "${#manifests[@]} manifests tracked (reference model and example vehicle)"; else fail "expected at least two robot.yaml files, found ${#manifests[@]}"; fi
if "${FIELDPROOF[@]}" manifest validate "${manifests[@]}"; then pass "fieldproof manifest validate on every robot.yaml"; else fail "fieldproof manifest validate"; fi
# Compared as JSON: prettier reformats the committed file.
if "${FIELDPROOF[@]}" manifest schema | python3 -c 'import json, sys; sys.exit(json.load(sys.stdin) != json.load(open(sys.argv[1])))' engine/schemas/robot.schema.json; then
  pass "engine/schemas/robot.schema.json is current"
else
  fail "engine/schemas/robot.schema.json is stale (make manifest-schema)"
fi
if uv tool run --from check-jsonschema==0.38.2 check-jsonschema --schemafile engine/schemas/robot.schema.json "${manifests[@]}" >/dev/null; then
  pass "check-jsonschema accepts every robot.yaml (the pre-commit hook)"
else
  fail "check-jsonschema rejects a robot.yaml"
fi
printf 'apiVersion: fieldproof.dev/v0alpha1\nkind: Robot\nmetadata: {name: bad}\nruntime: {image: x, entrypoint: {launch: {package: p, file: f}}, typo: 1}\n' >"$SCRATCH/bad.yaml"
if ! uv tool run --from check-jsonschema==0.38.2 check-jsonschema --schemafile engine/schemas/robot.schema.json "$SCRATCH/bad.yaml" >/dev/null 2>&1; then
  pass "check-jsonschema rejects an unknown runtime key"
else
  fail "check-jsonschema accepted an unknown runtime key"
fi
if ! "${FIELDPROOF[@]}" manifest validate "$SCRATCH/bad.yaml" >/dev/null 2>&1; then pass "fieldproof manifest validate rejects it too (exit 2)"; else fail "fieldproof manifest validate accepted a bad manifest"; fi

# ---------------------------------------------------------------------------------------------
section "2. Generator"
CONTEXT="$SCRATCH/context"
if "${FIELDPROOF[@]}" vehicle dockerfile "$MANIFEST" -o "$CONTEXT" >/dev/null \
  && [[ -f "$CONTEXT/Dockerfile" && -f "$CONTEXT/entrypoint.sh" && -f "$CONTEXT/robot.yaml" ]]; then
  pass "fieldproof vehicle dockerfile writes Dockerfile, entrypoint.sh and robot.yaml"
else
  fail "fieldproof vehicle dockerfile"
fi
if command -v hadolint >/dev/null 2>&1; then
  if hadolint --config .hadolint.yaml "$CONTEXT/Dockerfile"; then pass "hadolint (local) on the generated Dockerfile"; else fail "hadolint (local)"; fi
elif docker run --rm -i -v "$CONTEXT:/mnt:ro" "$HADOLINT_IMAGE" hadolint --config /dev/stdin /mnt/Dockerfile <.hadolint.yaml; then
  pass "hadolint (container ${HADOLINT_IMAGE%%@*}) on the generated Dockerfile"
else
  fail "hadolint on the generated Dockerfile"
fi
if command -v shellcheck >/dev/null 2>&1; then
  if shellcheck --severity=style --shell=bash "$CONTEXT/entrypoint.sh"; then pass "shellcheck on the generated entry point"; else fail "shellcheck on the entry point"; fi
elif docker run --rm -v "$CONTEXT:/mnt:ro" "$SHELLCHECK_IMAGE" --severity=style --shell=bash /mnt/entrypoint.sh; then
  pass "shellcheck (container) on the generated entry point"
else
  fail "shellcheck on the entry point"
fi
if grep -q '^FROM [$]{BASE_IMAGE} AS build$' "$CONTEXT/Dockerfile" && grep -q '^FROM [$]{BASE_IMAGE} AS runtime$' "$CONTEXT/Dockerfile" \
  && grep -q 'colcon build --merge-install' "$CONTEXT/Dockerfile" && grep -q 'rosdep update --rosdistro' "$CONTEXT/Dockerfile"; then
  pass "two stages, rosdep against the pinned rosdistro, colcon --merge-install"
else
  fail "the Dockerfile lacks a stage, the rosdep update or the colcon build"
fi
if python3 - "$CONTEXT/robot.yaml" "$MANIFEST" <<'PY'
import sys, yaml
resolved = yaml.safe_load(open(sys.argv[1]))
assert "extends" not in resolved, "extends must be resolved"
assert resolved["kind"] == "Robot" and resolved["runtime"]["entrypoint"]
assert len(resolved["sensors"]) == 4 and resolved["drive"]["command"]["ros_topic"] == "cmd_vel"
PY
then pass "the resolved manifest in the context has the model's sections and no extends"; else fail "the resolved manifest is wrong"; fi

# ---------------------------------------------------------------------------------------------
section "3. Image"
IMAGE="$(python3 -c 'import sys, yaml; print(yaml.safe_load(open(sys.argv[1]))["runtime"]["image"])' "$MANIFEST")"
if [[ "$STATIC" -eq 1 ]]; then
  skip "image build and checks (--static)"
elif [[ "$NO_BUILD" -eq 1 ]] && ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  fail "--no-build but $IMAGE is not present"
else
  if [[ "$NO_BUILD" -eq 0 ]]; then
    start=$SECONDS
    if "${FIELDPROOF[@]}" vehicle build "$MANIFEST" --context "$CONTEXT" --base-image "$BASE_IMAGE" >"$SCRATCH/build.log" 2>&1; then
      pass "fieldproof vehicle build $MANIFEST ($((SECONDS - start)) s)"
    else
      tail -40 "$SCRATCH/build.log"
      fail "fieldproof vehicle build (log above)"
    fi
  else
    pass "using the present image $IMAGE (--no-build)"
  fi
  if docker image inspect "$IMAGE" >/dev/null 2>&1; then
    run() { docker run --rm --entrypoint /usr/local/bin/fieldproof-vehicle-entrypoint "$IMAGE" "$@"; }
    if [[ "$(run id -un)" == "fieldproof" ]]; then pass "runs as the non-root user fieldproof"; else fail "does not run as fieldproof"; fi
    if run test -f /opt/fieldproof/robot.yaml -a -f /opt/vehicle/setup.bash -a -f /opt/vehicle/.fieldproof-exec-deps.txt; then
      pass "carries the manifest, the merged install space and the run-time dependency list"
    else
      fail "manifest, install space or dependency list missing"
    fi
    if ! run bash -c 'command -v gcc || command -v colcon || command -v rosdep' >/dev/null 2>&1; then
      pass "the runtime stage has no compiler, colcon or rosdep"
    else
      fail "the runtime stage carries build tools"
    fi
    if [[ "$(run ros2 pkg prefix ugv_nav2_bringup)" == "/opt/vehicle" ]] \
      && run test -f /opt/vehicle/share/ugv_nav2_bringup/config/nav2.yaml -a -f /opt/vehicle/share/ugv_nav2_bringup/config/slam.yaml; then
      pass "ros2 finds ugv_nav2_bringup in /opt/vehicle with its parameter files"
    else
      fail "ugv_nav2_bringup or its parameter files are not installed"
    fi
    if run ros2 pkg prefix nav2_bt_navigator >/dev/null && run ros2 pkg prefix nav2_mppi_controller >/dev/null \
      && run ros2 pkg prefix slam_toolbox >/dev/null; then
      pass "rosdep installed the run-time dependencies (Nav2 servers, slam_toolbox)"
    else
      fail "a Nav2 server or slam_toolbox is missing"
    fi
    if ! run ros2 pkg prefix nav2_bringup >/dev/null 2>&1 && ! run ros2 pkg prefix rviz2 >/dev/null 2>&1; then
      pass "neither nav2_bringup (Gazebo, TurtleBot models) nor rviz2 came along"
    else
      fail "nav2_bringup or rviz2 is installed"
    fi
    if ! run ros2 pkg prefix rplidar_ros >/dev/null 2>&1; then
      pass "the field drivers are not installed (conditional dependencies, UGV_TARGET unset)"
    else
      fail "rplidar_ros is installed in the simulation image"
    fi
    if run ros2 launch ugv_nav2_bringup bringup.launch.py --show-args 2>/dev/null | grep -q "'drivers'"; then
      pass "the bringup launch file resolves and declares the drivers argument"
    else
      fail "ros2 launch --show-args on bringup.launch.py"
    fi
    labels="$(docker image inspect "$IMAGE" --format '{{json .Config.Labels}}')"
    if python3 - "$labels" "$BASE_IMAGE" <<'PY'
import json, sys
labels = json.loads(sys.argv[1])
assert labels["org.opencontainers.image.title"] == "fieldproof-vehicle-ugv_nav2", labels
assert labels["io.fieldproof.vehicle"] == "ugv_nav2" and labels["io.fieldproof.tier"] == "T1"
assert labels["org.opencontainers.image.base.name"] == sys.argv[2], labels["org.opencontainers.image.base.name"]
assert len(labels["io.fieldproof.rosdistro-commit"]) == 40
assert labels["org.opencontainers.image.source"].startswith("https://github.com/")
PY
    then pass "OCI and Fieldproof labels"; else fail "labels"; fi
    cmd="$(docker image inspect "$IMAGE" --format '{{json .Config.Cmd}}')"
    if [[ "$cmd" == '["ros2","launch","ugv_nav2_bringup","bringup.launch.py","use_sim_time:=true","drivers:=false"]' ]]; then
      pass "the default command is the manifest's entrypoint with the simulation arguments"
    else
      fail "default command: $cmd"
    fi
    health="$(docker image inspect "$IMAGE" --format '{{json .Config.Healthcheck.Test}}')"
    if run test -x /usr/local/bin/fieldproof-vehicle-ready \
      && [[ "$health" == '["CMD","/usr/local/bin/fieldproof-vehicle-entrypoint","/usr/local/bin/fieldproof-vehicle-ready","--timeout","10"]' ]]; then
      pass "carries the readiness probe, which the HEALTHCHECK runs through the entry point"
    else
      fail "readiness probe or HEALTHCHECK missing: $health"
    fi
    probe="$(run fieldproof-vehicle-ready --timeout 3 2>&1)" && probe_status=0 || probe_status=$?
    if [[ "$probe_status" -eq 1 && "$probe" == *"not ready after"*"9 of 9 pending"*"/bt_navigator lifecycle unavailable"*"/navigate_to_pose action no server"* ]]; then
      pass "the probe alone reports the 9 conditions pending and exits 1"
    else
      fail "the probe alone exited $probe_status: $probe"
    fi
  fi
fi

# ---------------------------------------------------------------------------------------------
section "4. Graph"
if [[ "$STATIC" -eq 1 || "$NO_GRAPH" -eq 1 ]]; then
  skip "the vehicle on the network (--static or --no-graph)"
elif ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  skip "no image to run"
else
  docker network inspect "$NETWORK" >/dev/null 2>&1 || docker network create "$NETWORK" >/dev/null
  domain=$((RANDOM % 100 + 100))
  if "${FIELDPROOF[@]}" vehicle run "$MANIFEST" --name "$CONTAINER" --network "$NETWORK" --domain-id "$domain" --detach >/dev/null; then
    pass "fieldproof vehicle run starts the container (ROS_DOMAIN_ID=$domain)"
  else
    fail "fieldproof vehicle run"
  fi
  if docker inspect "$CONTAINER" --format '{{.HostConfig.NanoCpus}} {{.HostConfig.Memory}}' | grep -q '^2000000000 2147483648$'; then
    pass "the container carries the manifest's budget (2 CPUs, 2 GiB)"
  else
    fail "resource budget not applied: $(docker inspect "$CONTAINER" --format '{{.HostConfig.NanoCpus}} {{.HostConfig.Memory}}')"
  fi
  # Observed from another container of the same image on the same network, so a passing check
  # means the vehicle joined the ROS graph over DDS: its nodes are listed and their lifecycle
  # services answer. SLAM Toolbox activates on its own; Nav2's controller server blocks in its
  # activation until the bridge publishes the odom -> base_link transform, so the Nav2 servers
  # stay `inactive` here and reach `active` in the integration test with the simulator (P1-T04,
  # session 2), which is what the manifest's `ready` conditions describe.
  # The probe's fresh ROS daemon needs a few seconds of discovery, longer on a loaded runner,
  # so the node list is taken without the daemon after a spin and the whole probe is retried
  # until every expected node is there.
  expected=(/slam_toolbox /bt_navigator /controller_server /planner_server /behavior_server /velocity_smoother /collision_monitor /lifecycle_manager_navigation)
  probe() {
    docker run --rm --network "$NETWORK" --env "ROS_DOMAIN_ID=$domain" --entrypoint /usr/local/bin/fieldproof-vehicle-entrypoint "$IMAGE" \
      bash -c 'nodes=$(timeout 60 ros2 node list --no-daemon --spin-time 10 2>/dev/null | tr "\n" " "); printf "nodes: %s\n" "$nodes"; for n in /slam_toolbox /bt_navigator /planner_server; do s=$(timeout 20 ros2 lifecycle get "$n" 2>/dev/null | cut -d" " -f1); printf "%s=%s\n" "$n" "${s:-none}"; done'
  }
  all_nodes_listed() {
    local node
    for node in "${expected[@]}"; do [[ " $1 " == *" $node "* ]] || return 1; done
  }
  start=$SECONDS
  result=""
  nodes=""
  while ((SECONDS - start < 180)); do
    result="$(probe || true)"
    nodes="$(sed -n 's/^nodes: //p' <<<"$result")"
    if all_nodes_listed "$nodes" && [[ "$result" == *"/slam_toolbox=active"* && "$result" == *"/bt_navigator=inactive"* && "$result" == *"/planner_server=inactive"* ]]; then break; fi
    sleep 5
  done
  for node in "${expected[@]}"; do
    if [[ " $nodes " == *" $node "* ]]; then pass "node $node is in the graph, seen from another container"; else fail "node $node is not in the graph ($nodes)"; fi
  done
  if [[ "$result" == *"/slam_toolbox=active"* ]]; then
    pass "SLAM Toolbox is active after $((SECONDS - start)) s"
  else
    fail "SLAM Toolbox is not active: $result"
  fi
  if [[ "$result" == *"/bt_navigator=inactive"* && "$result" == *"/planner_server=inactive"* ]]; then
    pass "Nav2's bt_navigator and planner_server answer their lifecycle services (configured, waiting for the bridge's TF to activate)"
  else
    fail "Nav2's lifecycle services do not answer: $result"
  fi
  # The probe's last line is its verdict (the earlier lines show discovery in progress).
  wait_out="$("${FIELDPROOF[@]}" vehicle wait "$MANIFEST" --name "$CONTAINER" --timeout 5 2>&1)" && wait_status=0 || wait_status=$?
  verdict="$(tail -n 1 <<<"$wait_out")"
  if [[ "$wait_status" -eq 1 && "$verdict" == "not ready after"*"/bt_navigator lifecycle inactive"* && "$verdict" != *"/slam_toolbox lifecycle"* ]]; then
    pass "fieldproof vehicle wait exits 1 without the bridge: Nav2 pending, SLAM Toolbox ready"
  else
    fail "fieldproof vehicle wait exited $wait_status: $wait_out"
  fi
  health_status="$(docker inspect "$CONTAINER" --format '{{.State.Health.Status}}')"
  if [[ "$health_status" == starting || "$health_status" == unhealthy ]]; then
    pass "Docker health is '$health_status' without the bridge"
  else
    fail "Docker health is '$health_status' without the bridge"
  fi
  if docker logs "$CONTAINER" 2>&1 | grep -q "Timed out waiting for transform from base_link to odom"; then
    pass "the controller server waits for the odom -> base_link transform (no bridge in this test)"
  else
    skip "the controller server did not log its wait for TF yet"
  fi
  if ! docker logs "$CONTAINER" 2>&1 | grep -qE "\[ERROR\]|process has died"; then
    pass "no node died and nothing logged at ERROR level"
  else
    docker logs "$CONTAINER" 2>&1 | grep -E "\[ERROR\]|process has died" | head -10
    fail "a node died or logged an error (above)"
  fi
  docker rm -f "$CONTAINER" >/dev/null
fi

printf '\n%d passed, %d failed\n' "$PASSED" "$FAILED"
[[ "$FAILED" -eq 0 ]]
