"""Shared helpers for rig camera profiles.

A *profile* pins down one camera's viewpoint so it can be reproduced after the
rig is disassembled and put back together. It lives in ``rig_tools/profiles/<name>/``:

    profile.json    metadata + ArUco reference corners + last homography
    reference.png   the golden frame, used by rig_check.py for overlay alignment
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

import cv2
import numpy as np

PROFILE_ROOT = Path(__file__).resolve().parent / "profiles"

# 4x4 has the fewest modules of any ArUco family, so each module covers the most
# pixels. That is what we want on a 320x240 recording stream.
ARUCO_DICTS = {
    "4x4_50": cv2.aruco.DICT_4X4_50,
    "4x4_100": cv2.aruco.DICT_4X4_100,
    "5x5_50": cv2.aruco.DICT_5X5_50,
    "6x6_50": cv2.aruco.DICT_6X6_50,
}


def die(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(2)


# --------------------------------------------------------------------------- camera


def open_camera(spec: str, width: int | None, height: int | None) -> cv2.VideoCapture:
    """Open ``spec`` as a camera. Accepts ``/dev/video0``, a bare index, or a video file."""
    if re.fullmatch(r"\d+", spec):
        cap = cv2.VideoCapture(int(spec))
    else:
        cap = cv2.VideoCapture(spec)
    if not cap.isOpened():
        die(f"cannot open camera {spec!r} (try: ls /dev/video*)")
    if width:
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    if height:
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    return cap


def read_settled(cap: cv2.VideoCapture, warmup: int = 12) -> np.ndarray:
    """Grab a frame after letting auto-exposure/white-balance settle."""
    frame = None
    for _ in range(max(1, warmup)):
        ok, f = cap.read()
        if ok:
            frame = f
    if frame is None:
        die("camera opened but returned no frames")
    return frame


def load_still(path: str) -> np.ndarray:
    """Load a single image, for testing the tools without hardware."""
    img = cv2.imread(path)
    if img is None:
        die(f"cannot read image {path!r}")
    return img


def grab(source: str, width: int | None, height: int | None, warmup: int = 12) -> np.ndarray:
    """Grab one frame from a camera, or read it straight from an image file."""
    if Path(source).suffix.lower() in {".png", ".jpg", ".jpeg", ".bmp"}:
        return load_still(source)
    cap = open_camera(source, width, height)
    try:
        return read_settled(cap, warmup)
    finally:
        cap.release()


# --------------------------------------------------------------------------- profile


@dataclass
class Profile:
    name: str
    camera: str = ""
    width: int = 0
    height: int = 0
    aruco_dict: str = "4x4_50"
    # marker id -> 4 corners [[x, y], ...] in the golden frame
    ref_corners: dict[str, list[list[float]]] = field(default_factory=dict)
    # rig_check comparison windows: a list of [x, y, w, h]. Null means the whole
    # frame. A bare [x, y, w, h] from an older profile is still accepted.
    roi: list | None = None
    # pass/fail thresholds, so the daily command needs no flags
    max_shift: float | None = None
    min_correlation: float | None = None
    created: str = ""
    notes: str = ""

    # ---- paths
    @staticmethod
    def dir_for(name: str) -> Path:
        return PROFILE_ROOT / name

    @property
    def dir(self) -> Path:
        return self.dir_for(self.name)

    @property
    def json_path(self) -> Path:
        return self.dir / "profile.json"

    @property
    def reference_path(self) -> Path:
        return self.dir / "reference.png"

    # ---- io
    @classmethod
    def exists(cls, name: str) -> bool:
        return (cls.dir_for(name) / "profile.json").is_file()

    @classmethod
    def load(cls, name: str) -> Profile:
        path = cls.dir_for(name) / "profile.json"
        if not path.is_file():
            die(f"profile {name!r} not found at {path}. Run without --verify to create it.")
        data = json.loads(path.read_text())
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self, reference: np.ndarray | None = None) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.json_path.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False))
        if reference is not None:
            cv2.imwrite(str(self.reference_path), reference)

    def load_reference(self) -> np.ndarray:
        img = cv2.imread(str(self.reference_path))
        if img is None:
            die(f"profile {self.name!r} has no reference.png; recreate it")
        return img

    # ---- aruco
    def corners_as_arrays(self) -> dict[int, np.ndarray]:
        return {int(k): np.asarray(v, dtype=np.float32) for k, v in self.ref_corners.items()}


# --------------------------------------------------------------------------- aruco


def make_detector(dict_name: str) -> cv2.aruco.ArucoDetector:
    if dict_name not in ARUCO_DICTS:
        die(f"unknown dictionary {dict_name!r}; choose from {', '.join(ARUCO_DICTS)}")
    dictionary = cv2.aruco.getPredefinedDictionary(ARUCO_DICTS[dict_name])
    params = cv2.aruco.DetectorParameters()
    # The rig runs a small, soft, slightly blurry stream. Subpixel refinement and a
    # permissive border distance recover markers that the defaults drop.
    params.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
    params.minMarkerPerimeterRate = 0.02
    params.adaptiveThreshWinSizeMin = 3
    params.adaptiveThreshWinSizeMax = 33
    params.adaptiveThreshWinSizeStep = 4
    return cv2.aruco.ArucoDetector(dictionary, params)


def detect_markers(frame: np.ndarray, dict_name: str) -> dict[int, np.ndarray]:
    """Return ``{marker_id: (4, 2) float32 corners}``."""
    detector = make_detector(dict_name)
    corners, ids, _ = detector.detectMarkers(frame)
    if ids is None:
        return {}
    return {int(i): c.reshape(4, 2).astype(np.float32) for c, i in zip(corners, ids.flatten())}


def homography_to_reference(
    live: dict[int, np.ndarray], ref: dict[int, np.ndarray]
) -> tuple[np.ndarray | None, list[int], float]:
    """Homography mapping *live* pixels onto the *reference* view.

    Returns ``(H, shared_ids, rms_reprojection_error_px)``.
    """
    shared = sorted(set(live) & set(ref))
    if len(shared) < 2:
        return None, shared, float("nan")
    src = np.concatenate([live[i] for i in shared]).astype(np.float32)
    dst = np.concatenate([ref[i] for i in shared]).astype(np.float32)
    H, _ = cv2.findHomography(src, dst, method=0)
    if H is None:
        return None, shared, float("nan")
    projected = cv2.perspectiveTransform(src.reshape(-1, 1, 2), H).reshape(-1, 2)
    rms = float(np.sqrt(((projected - dst) ** 2).sum(axis=1).mean()))
    return H, shared, rms


def describe_homography(H: np.ndarray, size: tuple[int, int]) -> dict[str, float]:
    """Summarise how far a homography moves the image, in human units."""
    w, h = size
    corners = np.array([[0, 0], [w, 0], [w, h], [0, h]], dtype=np.float32).reshape(-1, 1, 2)
    moved = cv2.perspectiveTransform(corners, H).reshape(-1, 2)
    delta = moved - corners.reshape(-1, 2)
    centre = delta.mean(axis=0)
    # scale/rotation from the affine part of the corner correspondence
    M, _ = cv2.estimateAffinePartial2D(corners.reshape(-1, 2), moved)
    if M is None:
        scale, rot = float("nan"), float("nan")
    else:
        scale = float(np.hypot(M[0, 0], M[1, 0]))
        rot = float(np.degrees(np.arctan2(M[1, 0], M[0, 0])))
    return {
        "dx_px": float(centre[0]),
        "dy_px": float(centre[1]),
        "max_corner_shift_px": float(np.linalg.norm(delta, axis=1).max()),
        "scale": scale,
        "rotation_deg": rot,
    }


def warp_to_reference(frame: np.ndarray, H: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    """Apply a profile homography so the frame matches the golden viewpoint."""
    return cv2.warpPerspective(frame, H, size, flags=cv2.INTER_LINEAR)


# --------------------------------------------------------------------------- display


def is_image_path(source: str) -> bool:
    return Path(source).suffix.lower() in {".png", ".jpg", ".jpeg", ".bmp"}


def panels(items: list[tuple[str, np.ndarray]], height: int = 380) -> np.ndarray:
    """Lay named frames out side by side, scaled to a common height."""
    tiles = []
    for title, img in items:
        scale = height / img.shape[0]
        tile = cv2.resize(img, (int(round(img.shape[1] * scale)), height), interpolation=cv2.INTER_NEAREST)
        cv2.rectangle(tile, (0, 0), (tile.shape[1] - 1, 20), (0, 0, 0), -1)
        cv2.putText(tile, title, (6, 15), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1)
        tiles.append(tile)
    canvas = np.hstack(tiles)
    for x in np.cumsum([t.shape[1] for t in tiles])[:-1]:
        cv2.line(canvas, (int(x), 0), (int(x), canvas.shape[0]), (60, 60, 60), 1)
    return canvas


def add_banner(canvas: np.ndarray, text: str, good: bool | None) -> np.ndarray:
    """Prepend a coloured status bar. ``good=None`` renders a neutral bar."""
    colour = (70, 70, 70) if good is None else ((0, 140, 0) if good else (0, 0, 160))
    bar = np.full((26, canvas.shape[1], 3), colour, dtype=np.uint8)
    cv2.putText(bar, text, (8, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
    return np.vstack([bar, canvas])


def show(window: str, canvas: np.ndarray) -> int:
    """imshow + waitKey, dying with a useful message when there is no display."""
    try:
        cv2.imshow(window, canvas)
    except cv2.error:
        die("no display available; use --no-display (or --quiet/--json) for a headless check")
    return cv2.waitKey(30) & 0xFF


def draw_markers(frame: np.ndarray, markers: dict[int, np.ndarray], colour=(0, 255, 0)) -> np.ndarray:
    out = frame.copy()
    for mid, c in markers.items():
        pts = c.astype(int)
        cv2.polylines(out, [pts], True, colour, 1)
        cv2.putText(out, str(mid), tuple(pts[0]), cv2.FONT_HERSHEY_SIMPLEX, 0.4, colour, 1)
    return out
