#!/usr/bin/env python3
r"""Draw a top-down SVG map of an SDF world (task P1-T02).

    world_map.py worlds/depot.sdf --output ../../docs/reference/worlds/depot-map.svg \
        [--resource-path DIR ...] [--check]

Every model in the world is drawn from its collision geometry (or its visuals when a link has no
collision, which is how the floor patches appear): boxes as rotated polygons (the footprint of a
pitched box is the convex hull of its eight projected corners, so ramps come out right),
cylinders and spheres as circles, meshes as a nominal marker with the model's name. Included
models (`model://<dir>`) are read from the directories on the resource paths, so the Fuel models
fetched by tools/assets/fetch_fuel.py appear with their real footprints. Fill colours follow the
top height of each shape; shapes that start above 1.2 m (lintels, upper rack beams) are dashed
outlines, since a ground robot passes under them; large shapes carry their link name; the
world's named frames are drawn with their heading. The output is deterministic, so the
committed map can be checked against the world with --check (what test/test_worlds.py does).

Only the standard library is used.
"""

from __future__ import annotations

import argparse
import math
import os
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

Vec = tuple[float, float, float]
Pose = tuple[Vec, Vec]  # (xyz, rpy)
Point = tuple[float, float]

SCALE_PX_PER_M = 18.0
MARGIN_M = 1.5
MESH_MARKER_RADIUS_M = 0.3
OVERHEAD_BOTTOM_M = 1.2
LABEL_AREA_M2 = 10.0
LEGEND = [
    ("floor", "surface patch (visual only)", 0.0),
    ("low", "up to 0.5 m", 0.5),
    ("mid", "0.5 m to 1.5 m", 1.5),
    ("tall", "1.5 m to 2.5 m", 2.5),
    ("wall", "above 2.5 m", math.inf),
]
STYLE = """
  .floor { fill: #e9e6dc; stroke: none; }
  .low { fill: #f2b760; stroke: #8a5a0f; stroke-width: 0.6; }
  .mid { fill: #d9833b; stroke: #7a3d10; stroke-width: 0.6; }
  .tall { fill: #8f6bbf; stroke: #45307a; stroke-width: 0.6; }
  .wall { fill: #3a4350; stroke: #1b2027; stroke-width: 0.6; }
  .mesh { fill: #f2b760; stroke: #8a5a0f; stroke-width: 0.6; stroke-dasharray: 2 1.5; }
  .overhead { fill: none; stroke: #3a4350; stroke-width: 0.8; stroke-dasharray: 3 2; }
  .grid { stroke: #b8c0c8; stroke-width: 0.5; }
  .axis { font: 10px sans-serif; fill: #51606c; }
  .label { font: 9px sans-serif; fill: #1b2027; }
  .frame { fill: #0a7d8c; stroke: #ffffff; stroke-width: 1; }
  .heading { stroke: #0a7d8c; stroke-width: 2; }
  .frame-label { font: bold 10px sans-serif; fill: #0a7d8c; }
  .title { font: bold 13px sans-serif; fill: #1b2027; }
  .legend { font: 10px sans-serif; fill: #1b2027; }
  .legend-box { stroke: #1b2027; stroke-width: 0.5; }
"""


@dataclass
class Shape:
    """A footprint to draw: a polygon or a circle, with its height range and labels."""

    kind: str  # polygon | circle | mesh
    points: list[Point] = field(default_factory=list)
    centre: Point = (0.0, 0.0)
    radius: float = 0.0
    top_z: float = 0.0
    bottom_z: float = 0.0
    label: str = ""  # model name
    name: str = ""  # link name

    def area(self) -> float:
        """Footprint area in square metres (shoelace formula for polygons)."""
        if self.kind != "polygon" or len(self.points) < 3:
            return math.pi * self.radius * self.radius
        pts = self.points
        pairs = zip(pts, pts[1:] + pts[:1], strict=True)
        return 0.5 * abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in pairs))

    def centroid(self) -> Point:
        """Mean of the polygon's vertices, or the circle's centre."""
        if self.kind != "polygon" or not self.points:
            return self.centre
        n = len(self.points)
        return (sum(p[0] for p in self.points) / n, sum(p[1] for p in self.points) / n)

    def overhead(self) -> bool:
        """Whether a ground robot passes under the shape."""
        return self.bottom_z > OVERHEAD_BOTTOM_M


@dataclass
class Frame:
    """A named world frame: a spawn point or a goal."""

    name: str
    x: float
    y: float
    yaw: float


# ------------------------------------------------------------------------------------- geometry
def parse_pose(element: ET.Element | None) -> Pose:
    """Read an SDF <pose> (x y z roll pitch yaw); the identity when absent."""
    text = (element.text or "").strip() if element is not None else ""
    if not text:
        return (0.0, 0.0, 0.0), (0.0, 0.0, 0.0)
    values = [float(v) for v in text.split()]
    if len(values) != 6:
        raise ValueError(f"pose needs six numbers, got {text!r}")
    return (values[0], values[1], values[2]), (values[3], values[4], values[5])


def rotation(rpy: Vec) -> list[list[float]]:
    """Rotation matrix of SDF roll-pitch-yaw (R = Rz(yaw) Ry(pitch) Rx(roll))."""
    r, p, y = rpy
    cr, sr = math.cos(r), math.sin(r)
    cp, sp = math.cos(p), math.sin(p)
    cy, sy = math.cos(y), math.sin(y)
    return [
        [cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
        [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
        [-sp, cp * sr, cp * cr],
    ]


def apply(pose: Pose, point: Vec) -> Vec:
    """Transform a point from the frame `pose` describes into its parent frame."""
    (tx, ty, tz), rpy = pose
    m = rotation(rpy)
    x, y, z = point
    return (
        m[0][0] * x + m[0][1] * y + m[0][2] * z + tx,
        m[1][0] * x + m[1][1] * y + m[1][2] * z + ty,
        m[2][0] * x + m[2][1] * y + m[2][2] * z + tz,
    )


def compose(outer: Pose, inner: Pose) -> Pose:
    """Pose of `inner` expressed in the frame `outer` is expressed in."""
    _, outer_rpy = outer
    inner_xyz, inner_rpy = inner
    position = apply(outer, inner_xyz)
    m = rotation(outer_rpy)
    n = rotation(inner_rpy)
    c = [[sum(m[i][k] * n[k][j] for k in range(3)) for j in range(3)] for i in range(3)]
    pitch = -math.asin(max(-1.0, min(1.0, c[2][0])))
    roll = math.atan2(c[2][1], c[2][2])
    yaw = math.atan2(c[1][0], c[0][0])
    return position, (roll, pitch, yaw)


def convex_hull(points: list[Point]) -> list[Point]:
    """Andrew's monotone chain; the result runs counter-clockwise."""
    pts = sorted(set(points))
    if len(pts) <= 2:
        return pts

    def cross(o: Point, a: Point, b: Point) -> float:
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: list[Point] = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper: list[Point] = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def geometry_shape(geometry: ET.Element, pose: Pose, label: str, name: str) -> Shape | None:
    """Footprint of one <geometry> placed at `pose` (world frame); None for planes."""
    box = geometry.find("box/size")
    if box is not None and box.text:
        sx, sy, sz = (float(v) / 2 for v in box.text.split())
        corners = [apply(pose, (x, y, z)) for x in (-sx, sx) for y in (-sy, sy) for z in (-sz, sz)]
        return Shape(
            "polygon",
            points=convex_hull([(c[0], c[1]) for c in corners]),
            top_z=max(c[2] for c in corners),
            bottom_z=min(c[2] for c in corners),
            label=label,
            name=name,
        )
    centre = pose[0]
    xy = (centre[0], centre[1])
    cylinder = geometry.find("cylinder")
    if cylinder is not None:
        radius = float(cylinder.findtext("radius", "0"))
        half = float(cylinder.findtext("length", "0")) / 2
        return Shape("circle", centre=xy, radius=radius, top_z=centre[2] + half,
                     bottom_z=centre[2] - half, label=label, name=name)  # fmt: skip
    sphere = geometry.find("sphere/radius")
    if sphere is not None and sphere.text:
        radius = float(sphere.text)
        return Shape("circle", centre=xy, radius=radius, top_z=centre[2] + radius,
                     bottom_z=centre[2] - radius, label=label, name=name)  # fmt: skip
    if geometry.find("mesh") is not None:
        return Shape("mesh", centre=xy, radius=MESH_MARKER_RADIUS_M, top_z=centre[2],
                     bottom_z=centre[2], label=label, name=name)  # fmt: skip
    return None  # planes and heightmaps are the ground itself


# ------------------------------------------------------------------------------------- SDF
def model_dir_for(uri: str, resource_paths: list[Path]) -> Path:
    """Directory of a `model://<dir>` URI on the resource paths."""
    name = uri.removeprefix("model://").split("/")[0]
    for root in resource_paths:
        candidate = root / name
        if (candidate / "model.config").is_file():
            return candidate
    searched = ", ".join(str(p) for p in resource_paths) or "no resource path given"
    raise FileNotFoundError(f"{uri}: no model directory with a model.config under {searched}")


def included_model(uri: str, resource_paths: list[Path]) -> ET.Element:
    """The <model> element of an included model, from the SDF its model.config names."""
    directory = model_dir_for(uri, resource_paths)
    config = ET.parse(directory / "model.config").getroot()  # noqa: S314
    sdf_files = [e.text.strip() for e in config.findall("sdf") if e.text]
    if not sdf_files:
        raise FileNotFoundError(f"{directory}/model.config names no sdf file")
    root = ET.parse(directory / sdf_files[-1]).getroot()  # noqa: S314
    model = root.find("model")
    if model is None:
        raise ValueError(f"{directory}/{sdf_files[-1]} holds no <model>")
    return model


def model_shapes(model: ET.Element, model_pose: Pose, label: str) -> list[Shape]:
    """Footprints of every collision (or visual, for links without one) of a model."""
    shapes: list[Shape] = []
    for link in model.findall("link"):
        link_pose = compose(model_pose, parse_pose(link.find("pose")))
        elements = link.findall("collision") or link.findall("visual")
        for element in elements:
            geometry = element.find("geometry")
            if geometry is None:
                continue
            pose = compose(link_pose, parse_pose(element.find("pose")))
            shape = geometry_shape(geometry, pose, label, link.get("name", ""))
            if shape is not None:
                shapes.append(shape)
    for nested in model.findall("model"):
        nested_pose = compose(model_pose, parse_pose(nested.find("pose")))
        shapes.extend(model_shapes(nested, nested_pose, label))
    return shapes


def world_shapes(world: ET.Element, resource_paths: list[Path]) -> tuple[list[Shape], list[Frame]]:
    """Every footprint of the world's models and includes, and its named frames."""
    shapes: list[Shape] = []
    for model in world.findall("model"):
        shapes.extend(model_shapes(model, parse_pose(model.find("pose")), model.get("name", "")))
    for include in world.findall("include"):
        uri = (include.findtext("uri") or "").strip()
        name = (include.findtext("name") or uri.removeprefix("model://")).strip()
        model = included_model(uri, resource_paths)
        shapes.extend(model_shapes(model, parse_pose(include.find("pose")), name))
    frames = []
    for frame in world.findall("frame"):
        (x, y, _), (_, _, yaw) = parse_pose(frame.find("pose"))
        frames.append(Frame(frame.get("name", ""), x, y, yaw))
    return shapes, frames


# ------------------------------------------------------------------------------------- SVG
def height_class(shape: Shape) -> str:
    """CSS class of a shape: by top height, or mesh/overhead."""
    if shape.kind == "mesh":
        return "mesh"
    if shape.overhead():
        return "overhead"
    for name, _, limit in LEGEND:
        if shape.top_z <= limit + 1e-9:
            return name
    return "wall"


def fmt(value: float) -> str:
    """Two decimals at most, no trailing zeros, no negative zero."""
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


class Canvas:
    """World-to-page mapping for one map (metres to pixels, y up to y down)."""

    def __init__(self, min_x: float, max_y: float) -> None:
        self.min_x = min_x
        self.max_y = max_y

    def x(self, x: float) -> float:
        """Page x of a world x."""
        return 40 + (x - self.min_x) * SCALE_PX_PER_M

    def y(self, y: float) -> float:
        """Page y of a world y (north up)."""
        return 30 + (self.max_y - y) * SCALE_PX_PER_M

    def at(self, x: float, y: float) -> str:
        """A world point as an SVG `x,y` pair."""
        return f"{fmt(self.x(x))},{fmt(self.y(y))}"


def bounds(shapes: list[Shape], frames: list[Frame]) -> tuple[int, int, int, int]:
    """Integer world bounds (min_x, max_x, min_y, max_y) with a margin around everything."""
    xs = [p[0] for s in shapes for p in s.points] + [f.x for f in frames]
    ys = [p[1] for s in shapes for p in s.points] + [f.y for f in frames]
    for s in shapes:
        if s.radius:
            xs += [s.centre[0] - s.radius, s.centre[0] + s.radius]
            ys += [s.centre[1] - s.radius, s.centre[1] + s.radius]
    if not xs:
        raise ValueError("the world has nothing to draw")
    return (
        math.floor(min(xs) - MARGIN_M),
        math.ceil(max(xs) + MARGIN_M),
        math.floor(min(ys) - MARGIN_M),
        math.ceil(max(ys) + MARGIN_M),
    )


def render_grid(c: Canvas, min_x: int, max_x: int, min_y: int, max_y: int) -> list[str]:
    """Grid lines every 5 m with coordinate labels."""
    out = []
    for gx in range(math.ceil(min_x / 5) * 5, max_x + 1, 5):
        out.append(
            f'  <line class="grid" x1="{fmt(c.x(gx))}" y1="{fmt(c.y(max_y))}" '
            f'x2="{fmt(c.x(gx))}" y2="{fmt(c.y(min_y))}"/>'
        )
        out.append(
            f'  <text class="axis" x="{fmt(c.x(gx))}" y="{fmt(c.y(min_y) + 12)}" '
            f'text-anchor="middle">{gx}</text>'
        )
    for gy in range(math.ceil(min_y / 5) * 5, max_y + 1, 5):
        out.append(
            f'  <line class="grid" x1="{fmt(c.x(min_x))}" y1="{fmt(c.y(gy))}" '
            f'x2="{fmt(c.x(max_x))}" y2="{fmt(c.y(gy))}"/>'
        )
        out.append(
            f'  <text class="axis" x="{fmt(c.x(min_x) - 6)}" y="{fmt(c.y(gy) + 3)}" '
            f'text-anchor="end">{gy}</text>'
        )
    return out


def render_shapes(c: Canvas, shapes: list[Shape]) -> list[str]:
    """Lowest shapes first so walls stay on top; overhead outlines last."""
    out = []
    labelled: set[str] = set()
    for shape in sorted(shapes, key=lambda s: (s.overhead(), s.kind == "mesh", s.top_z)):
        cls = height_class(shape)
        if shape.kind == "polygon":
            points = " ".join(c.at(x, y) for x, y in shape.points)
            out.append(f'  <polygon class="{cls}" points="{points}"/>')
        else:
            out.append(
                f'  <circle class="{cls}" cx="{fmt(c.x(shape.centre[0]))}" '
                f'cy="{fmt(c.y(shape.centre[1]))}" r="{fmt(shape.radius * SCALE_PX_PER_M)}"/>'
            )
        if shape.kind == "mesh" and shape.label not in labelled:
            labelled.add(shape.label)
            out.append(
                f'  <text class="label" x="{fmt(c.x(shape.centre[0]) + 6)}" '
                f'y="{fmt(c.y(shape.centre[1]) + 3)}">{shape.label}</text>'
            )
        elif cls in ("low", "mid", "tall") and shape.area() >= LABEL_AREA_M2:
            cx, cy = shape.centroid()
            out.append(
                f'  <text class="label" x="{fmt(c.x(cx))}" y="{fmt(c.y(cy) + 14)}" '
                f'text-anchor="middle">{shape.name}</text>'
            )
    return out


def render_frames(c: Canvas, frames: list[Frame]) -> list[str]:
    """Named frames as a dot with a heading line and a label."""
    out = []
    for frame in frames:
        hx, hy = frame.x + 0.8 * math.cos(frame.yaw), frame.y + 0.8 * math.sin(frame.yaw)
        out.append(
            f'  <line class="heading" x1="{fmt(c.x(frame.x))}" y1="{fmt(c.y(frame.y))}" '
            f'x2="{fmt(c.x(hx))}" y2="{fmt(c.y(hy))}"/>'
        )
        out.append(
            f'  <circle class="frame" cx="{fmt(c.x(frame.x))}" cy="{fmt(c.y(frame.y))}" r="5"/>'
        )
        out.append(
            f'  <text class="frame-label" x="{fmt(c.x(frame.x) + 8)}" '
            f'y="{fmt(c.y(frame.y) - 6)}">{frame.name}</text>'
        )
    return out


def render_legend(top: float) -> list[str]:
    """The legend below the map."""
    out = [
        f'  <text class="legend" x="40" y="{fmt(top)}" font-weight="bold">'
        "Top height of the collision geometry</text>"
    ]
    for i, (name, description, _) in enumerate(LEGEND):
        y = top + 16 + i * 20
        out.append(
            f'  <rect class="{name} legend-box" x="40" y="{fmt(y - 10)}" width="14" height="14"/>'
        )
        out.append(f'  <text class="legend" x="60" y="{fmt(y + 1)}">{description}</text>')
    y = top + 16 + len(LEGEND) * 20
    out.append(f'  <circle class="mesh" cx="47" cy="{fmt(y - 3)}" r="7"/>')
    out.append(
        f'  <text class="legend" x="60" y="{fmt(y + 1)}">mesh collision '
        f"(nominal {fmt(MESH_MARKER_RADIUS_M)} m marker)</text>"
    )
    out.append(f'  <rect class="overhead" x="40" y="{fmt(y + 10)}" width="14" height="14"/>')
    out.append(
        f'  <text class="legend" x="60" y="{fmt(y + 21)}">starts above {fmt(OVERHEAD_BOTTOM_M)} m '
        "(lintel, rack beam): a ground robot passes underneath</text>"
    )
    out.append(f'  <circle class="frame" cx="47" cy="{fmt(y + 37)}" r="5"/>')
    out.append(
        f'  <text class="legend" x="60" y="{fmt(y + 41)}">named frame with heading '
        "(spawn point or goal)</text>"
    )
    return out


def render(world_name: str, shapes: list[Shape], frames: list[Frame], source: str) -> str:
    """The SVG document."""
    min_x, max_x, min_y, max_y = bounds(shapes, frames)
    legend_h = 20 * (len(LEGEND) + 3)
    width = (max_x - min_x) * SCALE_PX_PER_M + 60
    height = (max_y - min_y) * SCALE_PX_PER_M + 60 + legend_h
    c = Canvas(min_x, max_y)
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{fmt(width)}" height="{fmt(height)}" '
        f'viewBox="0 0 {fmt(width)} {fmt(height)}">',
        f"  <!-- generated by sim/fieldproof_worlds/scripts/world_map.py from {source}; "
        "do not edit -->",
        f"  <title>{world_name}: top view</title>",
        f"  <style>{STYLE}  </style>",
        '  <rect width="100%" height="100%" fill="#ffffff"/>',
        f'  <text class="title" x="40" y="18">{world_name}.sdf, top view '
        "(metres, x east, y north)</text>",
    ]
    out += render_grid(c, min_x, max_x, min_y, max_y)
    out += render_shapes(c, shapes)
    out += render_frames(c, frames)
    out += render_legend(c.y(min_y) + 30)
    out.append("</svg>")
    return "\n".join(out) + "\n"


def build_map(world_file: Path, resource_paths: list[Path]) -> str:
    """Parse the world and render its map."""
    root = ET.parse(world_file).getroot()  # noqa: S314
    world = root.find("world")
    if world is None:
        raise ValueError(f"{world_file} holds no <world>")
    shapes, frames = world_shapes(world, resource_paths)
    return render(world.get("name", world_file.stem), shapes, frames, world_file.name)


def default_resource_paths() -> list[Path]:
    """Directories from GZ_SIM_RESOURCE_PATH and SDF_PATH."""
    paths: list[Path] = []
    for variable in ("GZ_SIM_RESOURCE_PATH", "SDF_PATH"):
        paths += [Path(p) for p in os.environ.get(variable, "").split(os.pathsep) if p]
    return paths


def main(argv: list[str] | None = None) -> int:
    """Command line entry point."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("world", type=Path, help="SDF world file")
    parser.add_argument(
        "--output", "-o", type=Path, required=True, help="SVG file to write (or to compare)"
    )
    parser.add_argument(
        "--resource-path",
        type=Path,
        action="append",
        default=[],
        help="directory holding model://<dir> models; repeatable "
        "(default: $GZ_SIM_RESOURCE_PATH and $SDF_PATH)",
    )
    parser.add_argument(
        "--check", action="store_true", help="exit 1 when --output differs from the generated map"
    )
    args = parser.parse_args(argv)
    resource_paths = list(args.resource_path) or default_resource_paths()
    try:
        svg = build_map(args.world, resource_paths)
    except (OSError, ValueError, ET.ParseError) as error:
        sys.stderr.write(f"world_map: {error}\n")
        return 1
    if args.check:
        current = args.output.read_text() if args.output.is_file() else ""
        if current != svg:
            sys.stderr.write(
                f"world_map: {args.output} is out of date; regenerate it with:\n"
                f"  {Path(sys.argv[0]).name} {args.world} -o {args.output}\n"
            )
            return 1
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(svg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
