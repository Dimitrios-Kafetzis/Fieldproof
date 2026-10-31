#!/usr/bin/env bash
# Assemble the M2 demo video (docs/reference/demo-m2.md) from the clips the scenarios recorded.
#
#   tools/media/m2_demo_video.sh RUNS_DIR OUT_DIR
#
# RUNS_DIR holds the runs listed in CLIPS below (runs/<run_id>/video/<camera>.mp4 and
# report.json); OUT_DIR receives fieldproof-m2-demo.mp4, m2-demo.gif and the intermediate
# segments. ffmpeg runs in the lscr.io/linuxserver/ffmpeg container with the host's DejaVu
# fonts mounted; python3 reads the reports. Nothing installs on the host.
#
# Each CLIPS row: segment name | run id | camera | start s | length s | title | caption.
# The verdict line under a clip comes from the run's report (its verdict, assertions, duration).
set -euo pipefail

RUNS_DIR="$(realpath "${1:?RUNS_DIR}")"
OUT_DIR="$(realpath -m "${2:?OUT_DIR}")"
FONTS=/usr/share/fonts/truetype/dejavu
IMAGE=lscr.io/linuxserver/ffmpeg:latest
FPS=30
BG=0x14191f
GREEN=0x58d68d
mkdir -p "$OUT_DIR/seg" "$OUT_DIR/txt"

ff() {  # ffmpeg in the container, with the runs, the output and the fonts mounted
  docker run --rm -v "$RUNS_DIR:/runs:ro" -v "$OUT_DIR:/out" -v "$FONTS:/fonts:ro" "$IMAGE" \
    -hide_banner -loglevel error -y "$@"
}
fp() { docker run --rm -v "$RUNS_DIR:/runs:ro" -v "$OUT_DIR:/out" --entrypoint ffprobe "$IMAGE" -v error "$@"; }
duration() { fp -show_entries format=duration -of csv=p=0 "$1"; }
text() { printf '%s' "$2" > "$OUT_DIR/txt/$1.txt"; echo "/out/txt/$1.txt"; }
ENC=(-r "$FPS" -c:v libx264 -preset slow -crf 19 -pix_fmt yuv420p -an -movflags +faststart)

# verdict RUN_ID: "PASS · 11 of 11 assertions passed · 38 s of simulation"
verdict() {
  python3 - "$RUNS_DIR/$1/report.json" <<'PY'
import json, sys
r = json.load(open(sys.argv[1]))
a = r.get("assertions") or []
passed = sum(1 for x in a if x.get("status") == "passed")
print(f"{r['verdict']}  ·  {passed} of {len(a)} assertions passed  ·  {r.get('sim_elapsed_s') or r.get('timeline_duration_s') or 0:.0f} s of simulation")
PY
}

# card NAME SECONDS TITLE SUBTITLE [FOOT]: a text card on the dark background (each line of the
# subtitle centred on its own)
card() {
  local name="$1" secs="$2" title="$3" sub="$4" foot="${5:-}"
  local t f filter i=0 line lines=()
  t=$(text "$name-title" "$title"); f=$(text "$name-foot" "$foot")
  filter="drawtext=expansion=none:fontfile=/fonts/DejaVuSans-Bold.ttf:textfile=$t:fontsize=54:fontcolor=white:x=(w-text_w)/2:y=250"
  mapfile -t lines <<< "$sub"
  for line in "${lines[@]}"; do
    filter+=",drawtext=expansion=none:fontfile=/fonts/DejaVuSans.ttf:textfile=$(text "$name-sub$i" "$line"):fontsize=30:fontcolor=0xc9d1d9:x=(w-text_w)/2:y=$((345 + 46 * i))"
    i=$((i + 1))
  done
  filter+=",drawtext=expansion=none:fontfile=/fonts/DejaVuSans.ttf:textfile=$f:fontsize=24:fontcolor=$GREEN:x=(w-text_w)/2:y=620"
  filter+=",fade=t=in:st=0:d=0.4,fade=t=out:st=$(python3 -c "print($secs-0.4)"):d=0.4"
  ff -f lavfi -i "color=c=$BG:s=1280x720:d=$secs:r=$FPS" -vf "$filter" "${ENC[@]}" "/out/seg/$name.mp4"
}

# clip NAME RUN CAMERA START LENGTH TITLE CAPTION: a recorded clip with a caption bar (LENGTH
# `full`: to the recording's end)
clip() {
  local name="$1" run="$2" camera="$3" start="$4" length="$5" title="$6" caption="$7"
  local src="/runs/$run/video/$camera.mp4" ti ca ve
  if [[ "$length" == full ]]; then length=$(python3 -c "print(round($(duration "$src") - $start, 2))"); fi
  ti=$(text "$name-title" "$title"); ca=$(text "$name-cap" "$caption"); ve=$(text "$name-ver" "$(verdict "$run")")
  ff -ss "$start" -t "$length" -i "$src" -vf \
    "scale=1280:720,drawbox=y=ih-124:w=iw:h=124:color=black@0.55:t=fill,\
drawtext=expansion=none:fontfile=/fonts/DejaVuSans-Bold.ttf:textfile=$ti:fontsize=34:fontcolor=white:x=40:y=h-108,\
drawtext=expansion=none:fontfile=/fonts/DejaVuSans.ttf:textfile=$ca:fontsize=22:fontcolor=0xdde3ea:x=40:y=h-66,\
drawtext=expansion=none:fontfile=/fonts/DejaVuSans.ttf:textfile=$ve:fontsize=22:fontcolor=$GREEN:x=40:y=h-34,\
drawtext=expansion=none:fontfile=/fonts/DejaVuSans.ttf:text='Fieldproof · M2 demo':fontsize=20:fontcolor=white@0.7:x=w-text_w-30:y=24,\
fade=t=in:st=0:d=0.4,fade=t=out:st=$(python3 -c "print($length-0.4)"):d=0.4" \
    "${ENC[@]}" "/out/seg/$name.mp4"
}

# pair NAME RUN_A RUN_B START LENGTH TITLE CAPTION: two recordings side by side
pair() {
  local name="$1" a="$2" b="$3" start="$4" length="$5" title="$6" caption="$7"
  local ti ca ve
  if [[ "$length" == full ]]; then length=$(python3 -c "print(round(min($(duration "/runs/$a/video/spectator.mp4"), $(duration "/runs/$b/video/spectator.mp4")) - $start, 2))"); fi
  ti=$(text "$name-title" "$title"); ca=$(text "$name-cap" "$caption"); ve=$(text "$name-ver" "$(verdict "$a")")
  ff -ss "$start" -t "$length" -i "/runs/$a/video/spectator.mp4" -ss "$start" -t "$length" -i "/runs/$b/video/spectator.mp4" \
    -filter_complex "[0:v]scale=640:360[a];[1:v]scale=640:360[b];[a][b]hstack,pad=1280:720:0:150:$BG,\
drawtext=expansion=none:fontfile=/fonts/DejaVuSans.ttf:text='run A':fontsize=26:fontcolor=white:x=20:y=112,\
drawtext=expansion=none:fontfile=/fonts/DejaVuSans.ttf:text='run B':fontsize=26:fontcolor=white:x=660:y=112,\
drawbox=y=ih-124:w=iw:h=124:color=black@0.55:t=fill,\
drawtext=expansion=none:fontfile=/fonts/DejaVuSans-Bold.ttf:textfile=$ti:fontsize=34:fontcolor=white:x=40:y=h-108,\
drawtext=expansion=none:fontfile=/fonts/DejaVuSans.ttf:textfile=$ca:fontsize=22:fontcolor=0xdde3ea:x=40:y=h-66,\
drawtext=expansion=none:fontfile=/fonts/DejaVuSans.ttf:textfile=$ve:fontsize=22:fontcolor=$GREEN:x=40:y=h-34,\
drawtext=expansion=none:fontfile=/fonts/DejaVuSans.ttf:text='Fieldproof · M2 demo':fontsize=20:fontcolor=white@0.7:x=w-text_w-30:y=24,\
fade=t=in:st=0:d=0.4,fade=t=out:st=$(python3 -c "print($length-0.4)"):d=0.4" \
    "${ENC[@]}" "/out/seg/$name.mp4"
}

# still NAME IMAGE SECONDS: an image (a card drawn elsewhere) held for a while
still() {
  local name="$1" image="$2" secs="$3"
  ff -loop 1 -framerate "$FPS" -t "$secs" -i "/out/$image" -vf \
    "scale=1280:720,fade=t=in:st=0:d=0.4,fade=t=out:st=$(python3 -c "print($secs-0.4)"):d=0.4" \
    "${ENC[@]}" "/out/seg/$name.mp4"
}

# concat NAME SEGMENTS...: the segments one after another
concat() {
  local name="$1"; shift
  : > "$OUT_DIR/$name.txt"
  for seg in "$@"; do echo "file '/out/seg/$seg.mp4'" >> "$OUT_DIR/$name.txt"; done
  ff -f concat -safe 0 -i "/out/$name.txt" -c copy "/out/$name.mp4"
}

# ------------------------------------------------------------------------------- the plan
# Start and length cut each recording to its drive (the recorder runs from the timeline's
# start to its end, so a clip opens on a vehicle standing still and closes on the teardown).
REPLAY_CARD="${REPLAY_CARD:-replay-card.png}"  # drawn by tools/media/replay_plot.py into OUT_DIR

card title 5 "Fieldproof" "Milestone M2: fleet, faults, two clocks
Every clip is a scenario with pass/fail assertions, recorded by the simulator itself" "v0.20.1 · make demo-m2"
card c-fleet 4 "A fleet of three" "Three UGVs with namespaces and resource budgets, one scenario:
the yard, the dock and the hall, each vehicle driving its own 4 m" "examples/scenarios/ugv_fleet.yaml"
clip fleet m2v-fleet overview "${FLEET_SS:-0}" "${FLEET_T:-full}" "A fleet of three UGVs, from the overview camera" \
  "ugv1 at the dock, ugv2 in the hall, ugv3 in the yard: one container per vehicle, one TF tree each"
card c-links 4 "A radio link cut" "The LTE link between ugv1 and its ground station is dropped for 30 s mid-drive:
the loss-of-link failsafe holds the vehicle until the heartbeat returns" "examples/scenarios/ugv_link_loss.yaml"
clip links "${LINKS_RUN:-m2v-links2}" "${LINKS_CAM:-hall}" "${LINKS_SS:-0}" "${LINKS_T:-full}" "A radio link cut under a driving vehicle" \
  "netem on the vehicle's link: 100 % loss for 30 s, then lte_rural again; the vehicle holds, then drives on"
card c-obstacle 4 "A crate in the path" "The timeline drops a crate in front of the vehicle once it moves;
Nav2 replans around it" "examples/scenarios/ugv_obstacle_replan.yaml"
clip obstacle m2v-obstacle spectator "${OBST_SS:-0}" "${OBST_T:-full}" "A crate dropped in the path, Nav2 replans" \
  "spawn_object from the timeline; the detour measured by path_efficiency, min_distance to the crate asserted"
card c-simctl 4 "The simulator paused, stepped, resumed" "The timeline freezes the simulator under the driving vehicle,
nudges it 500 physics steps, lets it go: the stack never notices" "examples/scenarios/ugv_sim_control.yaml"
clip simctl m2v-simctl spectator "${SIMCTL_SS:-0}" "${SIMCTL_T:-full}" "Pause, step, resume from the timeline" \
  "recorded in simulation time, as the stack sees it: the pauses took seconds of wall clock and no time here"
card c-lockstep 4 "Two lockstep runs" "The same scenario stepped in lockstep twice, the observer pacing every agent:
identical trajectories, compared by fieldproof compare" "examples/scenarios/ugv_lockstep.yaml"
pair lockstep m2v-lockstep-a m2v-lockstep-b "${LOCK_SS:-0}" "${LOCK_T:-full}" "Two lockstep runs, side by side" \
  "run A and run B of ugv_lockstep.yaml; their odometry and trajectories agree within tolerance"
card c-gps 4 "GNSS denied" "The fix is denied for 20 s and offset by the timeline, under a SLAM stack
that never reads it: the drive out and back is unaffected" "examples/scenarios/ugv_gps_denied.yaml"
clip gps m2v-gps spectator "${GPS_SS:-0}" "${GPS_T:-full}" "GNSS denied for 20 s, the stack localises on its lidar" \
  "gps.deny from the timeline; fix_lost and fix_back asserted on /gps/fix; the drive back in time"
card c-realtime 4 "Real-time locked" "The drive at the wall clock's pace, the clock's drift measured" "examples/scenarios/ugv_realtime.yaml"
clip realtime m2v-realtime spectator "${RT_SS:-0}" "${RT_T:-full}" "The drive real-time locked" \
  "time.mode realtime: the simulator paced to the wall clock, the drift against it a KPI"
card c-matrix 7 "A run matrix" "ugv_nav_matrix.yaml: the drive over seeds and CPU budgets, one run per case,
one JUnit document for CI, every run in the run database" "fieldproof matrix · 2 of 2 cases PASS in the demo's quick form, 12 of 12 nightly"
card c-replay 4 "A field log replayed" "No simulator: the player publishes the clock and the recorded topics into the stack,
slam_toolbox localises, the estimate is judged against the log's ground truth" "examples/scenarios/ugv_replay.yaml"
still replay "$REPLAY_CARD" 8
card c-onboarding 4 "An external vehicle repository" "fieldproof-example-vehicle: a team's own navigator, onboarded through a manifest
and run through the repository's Makefile, no Fieldproof source touched" "fieldproof vehicle init · docs/reference/onboarding.md"
clip hold m2v-hold spectator "${HOLD_SS:-0}" "${HOLD_T:-full}" "The example team's navigator holds behind a crate" \
  "obstacle_hold.yaml: a crate dropped on the line; the navigator holds, the crate is moved aside, the drive resumes"
clip dock m2v-dock spectator "${DOCK_SS:-0}" "${DOCK_T:-full}" "The example repository's dock-to-yard drive" \
  "dock_to_yard.yaml: ten assertions and four KPIs on a navigator written for the example, 250 lines of rclpy"
card outro 6 "Fieldproof" "Run the real stack. Prove it before the field.
github.com/Dimitrios-Kafetzis/Fieldproof" "M2 signed off with v0.20.1 · next: the cockpit (M3)"

concat fieldproof-m2-demo title c-fleet fleet c-links links c-obstacle obstacle c-simctl simctl c-lockstep lockstep \
  c-gps gps c-realtime realtime c-matrix c-replay replay c-onboarding hold dock outro
echo "video: $OUT_DIR/fieldproof-m2-demo.mp4 ($(duration /out/fieldproof-m2-demo.mp4) s)"

# The README GIF: a few seconds of the most telling recordings, one short label each, 640 px
# wide at 10 fps with one palette. gifpiece NAME RUN CAMERA START LENGTH LABEL.
gifpiece() {
  local name="$1" run="$2" camera="$3" start="$4" length="$5" label="$6" la
  la=$(text "gif-$name" "$label")
  ff -ss "$start" -t "$length" -i "/runs/$run/video/$camera.mp4" -vf \
    "scale=1280:720,drawtext=expansion=none:fontfile=/fonts/DejaVuSans-Bold.ttf:textfile=$la:fontsize=46:fontcolor=white:box=1:boxcolor=black@0.55:boxborderw=18:x=40:y=36" \
    -c:v libx264 -preset fast -crf 18 -pix_fmt yuv420p -r "$FPS" -an "/out/seg/gif-$name.mp4"
}
gifpiece fleet m2v-fleet overview "${GIF_FLEET_SS:-3}" 6 "A fleet of three UGVs"
gifpiece obstacle m2v-obstacle spectator "${GIF_OBST_SS:-4}" 7 "A crate dropped in the path: Nav2 replans"
gifpiece links "${LINKS_RUN:-m2v-links2}" "${LINKS_CAM:-hall}" "${GIF_LINKS_SS:-6}" 6 "The radio link cut: the vehicle holds"
gifpiece hold m2v-hold spectator "${GIF_HOLD_SS:-3}" 7 "An external repository's own navigator"
concat gif-source gif-fleet gif-obstacle gif-links gif-hold
ff -i /out/gif-source.mp4 -vf \
  "fps=10,scale=640:-1:flags=lanczos,split[s0][s1];[s0]palettegen=max_colors=160:stats_mode=diff[p];[s1][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle" \
  -loop 0 /out/m2-demo.gif
echo "gif: $OUT_DIR/m2-demo.gif ($(stat -c %s "$OUT_DIR/m2-demo.gif") bytes)"
