#!/usr/bin/env python
"""Alignment check: compare the live view against a saved golden frame.

No markers required, so this works on a camera you have already mounted -- but it
only measures translation, not rotation or scale. Use it as the cheap daily gate;
use rig_calib.py when you want a correction you can actually apply.

Running it opens a window. If the profile already exists you get the saved
reference next to the live view plus a blended overlay; if it does not, you get
the live view alone and SPACE saves it as the reference.

    python rig_tools/rig_check.py --camera /dev/video0 --profile top

Exit code 0 = aligned, 1 = drifted, so it can gate recording:

    python rig_tools/rig_check.py --camera /dev/video0 --profile top --quiet || exit 1
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import time

import cv2
import numpy as np

from rig_common import (
    Profile,
    add_banner,
    die,
    grab,
    is_image_path,
    open_camera,
    panels,
    show,
)


def _gray(img: np.ndarray) -> np.ndarray:
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float32)
    # The rig has a blown-out wall and a near-black table. Normalising keeps the
    # score about geometry rather than however the auto-exposure settled today.
    return (g - g.mean()) / (g.std() + 1e-6)


def _parse_one(spec: str, size: tuple[int, int]) -> tuple[int, int, int, int]:
    """Parse ``x,y,w,h``. Values <= 1 are read as fractions of the frame."""
    try:
        nums = [float(v) for v in spec.split(",")]
    except ValueError:
        die(f"bad region {spec!r}; expected x,y,w,h")
    if len(nums) != 4:
        die(f"bad region {spec!r}; expected 4 values x,y,w,h, got {len(nums)}")
    w, h = size
    if all(v <= 1.0 for v in nums):
        nums = [nums[0] * w, nums[1] * h, nums[2] * w, nums[3] * h]
    x, y, rw, rh = (int(round(v)) for v in nums)
    x, y = max(0, x), max(0, y)
    rw, rh = min(rw, w - x), min(rh, h - y)
    if rw < 8 or rh < 8:
        die(f"region {spec!r} is degenerate for a {w}x{h} frame")
    return x, y, rw, rh


def parse_rois(specs: list[str] | None, size: tuple[int, int]) -> list[tuple[int, int, int, int]]:
    """Parse repeated --roi flags. Each may hold several regions split by ``;``."""
    if not specs:
        return []
    out = []
    for spec in specs:
        for part in str(spec).split(";"):
            part = part.strip()
            if part:
                out.append(_parse_one(part, size))
    return out


def normalise_stored(roi, size: tuple[int, int]) -> list[tuple[int, int, int, int]]:
    """Read profile.roi, accepting both the old flat [x,y,w,h] and a list of them."""
    if not roi:
        return []
    if all(isinstance(v, (int, float)) for v in roi):  # legacy single region
        roi = [roi]
    return [_parse_one(",".join(str(v) for v in r), size) for r in roi]


def _crop(img: np.ndarray, roi) -> np.ndarray:
    x, y, w, h = roi
    return img[y : y + h, x : x + w]


def compare(reference: np.ndarray, live: np.ndarray, rois=None) -> dict:
    """Compare over one or more windows.

    Each window is scored independently, which is what makes several of them
    useful: a camera that moved shifts every window together, while a single
    bumped object only shifts the window containing it, and the per-region
    breakdown tells you which happened.
    """
    if live.shape != reference.shape:
        live = cv2.resize(live, (reference.shape[1], reference.shape[0]))
    regions = list(rois) if rois else [(0, 0, reference.shape[1], reference.shape[0])]

    per = []
    for r in regions:
        ref_g, live_g = _gray(_crop(reference, r)), _gray(_crop(live, r))
        (dx, dy), response = cv2.phaseCorrelate(ref_g, live_g)
        per.append(
            {
                "region": list(r),
                "dx_px": float(dx),
                "dy_px": float(dy),
                "shift_px": float(np.hypot(dx, dy)),
                "correlation": float((ref_g * live_g).mean()),
                "phase_response": float(response),
            }
        )

    weights = np.array([r[2] * r[3] for r in regions], dtype=float)
    weights /= weights.sum()
    # Aggregate conservatively: any window that moved, or that stopped matching,
    # should fail the check even if the others still look fine.
    return {
        "dx_px": float(sum(w * p["dx_px"] for w, p in zip(weights, per))),
        "dy_px": float(sum(w * p["dy_px"] for w, p in zip(weights, per))),
        "shift_px": max(p["shift_px"] for p in per),
        "correlation": min(p["correlation"] for p in per),
        "phase_response": min(p["phase_response"] for p in per),
        "roi": [list(r) for r in regions] if rois else None,
        "regions": per,
    }


def overlay(reference: np.ndarray, live: np.ndarray, rois=None, per_region=None) -> np.ndarray:
    """Reference in green, live in magenta. They grey out where they agree."""
    if live.shape != reference.shape:
        live = cv2.resize(live, (reference.shape[1], reference.shape[0]))
    r = cv2.cvtColor(reference, cv2.COLOR_BGR2GRAY)
    l = cv2.cvtColor(live, cv2.COLOR_BGR2GRAY)
    out = cv2.merge([l, r, l])
    for i, roi in enumerate(rois or []):
        x, y, w, h = roi
        colour = (0, 255, 255)
        if per_region and i < len(per_region):
            colour = (0, 255, 255) if per_region[i]["shift_px"] < 3 else (0, 128, 255)
        cv2.rectangle(out, (x, y), (x + w, y + h), colour, 1)
        cv2.putText(out, str(i), (x + 2, y + 10), cv2.FONT_HERSHEY_SIMPLEX, 0.3, colour, 1)
    return out


DEFAULT_MAX_SHIFT = 20.0
DEFAULT_MIN_CORRELATION = 0.80


def resolve_thresholds(args, profile: Profile | None) -> None:
    """Explicit flag wins, then the profile, then the built-in default."""
    if args.max_shift is None:
        args.max_shift = (profile.max_shift if profile else None) or DEFAULT_MAX_SHIFT
    if args.min_correlation is None:
        args.min_correlation = (profile.min_correlation if profile else None) or DEFAULT_MIN_CORRELATION


def evaluate(report: dict, args) -> bool:
    return report["shift_px"] <= args.max_shift and report["correlation"] >= args.min_correlation


def suggest_thresholds(args, profile: Profile, roi, n: int) -> int:
    """Measure the rig's own frame-to-frame variation and propose limits from it.

    Samples are spaced out on purpose: the numbers are only meaningful if the arm
    and the objects actually move between them. Back-to-back frames of a static
    scene all score the same and would suggest uselessly tight limits.
    """
    reference = profile.load_reference()
    cap = open_camera(args.camera, args.width, args.height)
    shifts, corrs = [], []
    total = n * args.suggest_interval
    print(f"sampling {n} frames, one every {args.suggest_interval:.1f}s ({total:.0f}s total)")
    print("MOVE THE ARM AND THE OBJECTS between samples - otherwise the result is meaningless.")
    try:
        for i in range(n):
            if i:
                time.sleep(args.suggest_interval)
            frame = None
            for _ in range(6):
                grabbed, f = cap.read()
                if grabbed:
                    frame = f
            if frame is None:
                continue
            r = compare(reference, frame, roi)
            shifts.append(r["shift_px"])
            corrs.append(r["correlation"])
            print(f"  {i + 1:3d}/{n}  shift {r['shift_px']:6.2f}  corr {r['correlation']:.3f}")
    finally:
        cap.release()

    if len(shifts) < 3:
        die("not enough samples; is the camera in use by another process?")
    s_max, c_min = max(shifts), min(corrs)
    if max(corrs) - c_min < 0.005 and max(shifts) - min(shifts) < 0.05:
        print(
            "\nwarning: every sample scored the same, so the scene never changed. "
            "Re-run and actually move the arm and the objects, or these limits will be "
            "far too tight and will reject normal recording."
        )
    # Leave headroom over the observed spread so normal scene changes never trip it,
    # but stay far below the tens of pixels a real remount produces.
    new_shift = max(2.0, round(s_max * 4, 1))
    new_corr = round(max(0.5, c_min - 0.06), 2)
    print(f"\nobserved over {len(shifts)} frames: shift <= {s_max:.2f} px, corr >= {c_min:.3f}")
    print(f"suggested: --max-shift {new_shift}  --min-correlation {new_corr}")
    print("\nstore them in the profile with:")
    print(
        f"  python rig_tools/rig_check.py --camera {args.camera} --profile {profile.name} "
        f"--update --max-shift {new_shift} --min-correlation {new_corr}"
    )
    return 0



def propose_regions(frames: list, max_regions: int = 3, cell: int = 16) -> list:
    """Pick comparison windows automatically from a burst of frames.

    A good window is scenery that holds still while everything else moves, and
    that has enough texture to correlate against. So score each cell of a grid by
    texture divided by temporal variation, and keep the best connected blobs.
    Feed it frames captured while the robot and the objects are actually moving --
    that motion is the signal that tells the two apart.
    """
    stack = np.stack([cv2.cvtColor(f, cv2.COLOR_BGR2GRAY).astype(np.float32) for f in frames])
    # Normalise each frame so a drifting exposure does not read as motion.
    stack = (stack - stack.mean((1, 2), keepdims=True)) / (stack.std((1, 2), keepdims=True) + 1e-6)
    motion = stack.std(0)
    texture = np.abs(cv2.Laplacian(stack.mean(0), cv2.CV_32F))

    h, w = motion.shape
    gh, gw = h // cell, w // cell
    m = motion[: gh * cell, : gw * cell].reshape(gh, cell, gw, cell).mean((1, 3))
    t = texture[: gh * cell, : gw * cell].reshape(gh, cell, gw, cell).mean((1, 3))
    score = t / (m + 0.05)

    # Keep cells that are both quiet and textured, relative to this scene.
    good = ((m < np.percentile(m, 55)) & (t > np.percentile(t, 45))).astype(np.uint8)
    if good.sum() < 4:
        good = (score > np.percentile(score, 75)).astype(np.uint8)
    good = cv2.morphologyEx(good, cv2.MORPH_CLOSE, np.ones((2, 2), np.uint8))

    n, lab, st, _ = cv2.connectedComponentsWithStats(good, 8)
    blobs = sorted(
        ((st[i], score[lab == i].mean()) for i in range(1, n) if st[i, 4] >= 4),
        key=lambda z: -z[1],
    )
    out = []
    for b, sc in blobs[:max_regions]:
        x, y, bw, bh = b[0] * cell, b[1] * cell, b[2] * cell, b[3] * cell
        if bw >= 2 * cell and bh >= cell:
            out.append(((x, y, bw, bh), float(sc), float(m[b[1] : b[1] + b[3], b[0] : b[0] + b[2]].mean())))
    return out


def suggest_roi(args, n: int) -> int:
    """Capture a burst, then print --roi flags for the stillest textured areas."""
    cap = open_camera(args.camera, args.width, args.height)
    frames = []
    print(f"sampling {n} frames, one every {args.suggest_interval:.1f}s "
          f"({n * args.suggest_interval:.0f}s total)")
    print("MOVE THE ROBOT AND THE OBJECTS the whole time - that motion is what marks")
    print("the areas to avoid. A still scene gives a useless answer.")
    try:
        for i in range(n):
            if i:
                time.sleep(args.suggest_interval)
            f = None
            for _ in range(6):
                grabbed, x = cap.read()
                if grabbed:
                    f = x
            if f is not None:
                frames.append(f)
                print(f"  {len(frames):3d}/{n}")
    finally:
        cap.release()

    if len(frames) < 5:
        die("need at least 5 frames; is the camera in use by another process?")

    proposed = propose_regions(frames)
    if not proposed:
        die("found no stable textured area. Everything in view moves, or the scene is "
            "featureless -- fix something textured in frame (a taped card, a jig, the "
            "robot base) and retry.")

    print(f"\nfound {len(proposed)} candidate window(s), stillest first:")
    flags = []
    for (x, y, w, h), sc, mo in proposed:
        print(f"  {x},{y},{w},{h}   score {sc:6.1f}   motion {mo:.3f}")
        flags.append(f"--roi {x},{y},{w},{h}")
    print("\nCHECK THE PREVIEW before trusting these - a region that merely happened to")
    print("hold still during the burst will cause false alarms later.")
    if args.diff_out:
        preview = overlay(frames[0], frames[-1], [p[0] for p in proposed])
        cv2.imwrite(args.diff_out, preview)
        print(f"wrote preview to {args.diff_out}")
    print("\nuse them with:")
    print(f"  python rig_tools/rig_check.py --camera {args.camera} --profile {args.profile} \\")
    print(f"      --update {' '.join(flags)}")
    return 0


def print_report(report: dict, ok: bool, name: str, args) -> None:
    print(f"{'OK  ' if ok else 'FAIL'} view {'matches' if ok else 'drifted from'} profile {name!r}")
    print(
        f"     shift       : dx {report['dx_px']:+.1f} px, dy {report['dy_px']:+.1f} px, "
        f"magnitude {report['shift_px']:.1f} px (limit {args.max_shift:.1f})"
    )
    print(f"     correlation : {report['correlation']:.3f} (min {args.min_correlation:.2f})")
    regions = report.get("regions") or []
    if len(regions) > 1:
        print(f"     windows     : {len(regions)}  (worst values shown above)")
        for i, p in enumerate(regions):
            print(
                f"       [{i}] {str(p['region']):<22} dx {p['dx_px']:+6.2f} dy {p['dy_px']:+6.2f}"
                f"  shift {p['shift_px']:5.2f}  corr {p['correlation']:.3f}"
            )
    else:
        print(f"     window      : {(report['roi'] or ['full frame'])[0]}")
    if not ok:
        print("\n     Nudge the mount and re-run; the window view makes it easier to see.")
        print("     Reference frame: " + str(Profile.dir_for(name) / "reference.png"))


def save_reference(args, frame: np.ndarray, fresh: bool) -> Profile:
    profile = Profile(name=args.profile) if fresh else Profile.load(args.profile)
    profile.camera = args.camera
    profile.height, profile.width = frame.shape[:2]
    profile.notes = args.notes or profile.notes
    rois = parse_rois(args.roi, (profile.width, profile.height))
    profile.roi = [list(r) for r in rois] if rois else profile.roi
    if args.max_shift is not None:
        profile.max_shift = args.max_shift
    if args.min_correlation is not None:
        profile.min_correlation = args.min_correlation
    profile.created = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    profile.save(reference=frame)
    print(f"{'created' if fresh else 'updated'} profile {profile.name!r} at {profile.dir}")
    print(f"  reference : {profile.reference_path} ({profile.width}x{profile.height})")
    print(f"  windows   : {profile.roi or 'full frame'}")
    print(
        f"  limits    : shift <= {profile.max_shift or DEFAULT_MAX_SHIFT}, "
        f"corr >= {profile.min_correlation or DEFAULT_MIN_CORRELATION}"
    )
    if not fresh:
        print("  note: golden frame replaced; earlier episodes came from the OLD view.")
    return profile


def run_window(args, profile: Profile | None) -> int:
    """Show the saved reference alongside the live view, or the live view alone."""
    cap = open_camera(args.camera, args.width, args.height)
    window = f"rig_check [{args.profile}]"
    reference = profile.load_reference() if profile else None
    rois = []
    if profile:
        rois = parse_rois(args.roi, (profile.width, profile.height)) or normalise_stored(
            profile.roi, (profile.width, profile.height)
        )
        print("reference | live | overlay (reference GREEN, live MAGENTA, aligned = grey)")
        print("SPACE = re-anchor to the current view, q or ESC = quit")
    else:
        print(f"no profile named {args.profile!r} yet - showing the live view only")
        print("SPACE = save the current view as the reference, q or ESC = quit")

    ok = False
    try:
        while True:
            grabbed, frame = cap.read()
            if not grabbed:
                continue

            if reference is not None:
                report = compare(reference, frame, rois)
                ok = evaluate(report, args)
                canvas = panels(
                    [
                        ("reference", reference),
                        ("live", frame),
                        ("overlay", overlay(reference, frame, rois, report.get("regions"))),
                    ]
                )
                status = (
                    f"{'OK' if ok else 'ADJUST'}   dx{report['dx_px']:+.1f} dy{report['dy_px']:+.1f}"
                    f"   |{report['shift_px']:.1f}px| (max {args.max_shift:.0f})"
                    f"   corr {report['correlation']:.3f} (min {args.min_correlation:.2f})"
                    "   [SPACE] re-anchor  [q] quit"
                )
                canvas = add_banner(canvas, status, ok)
            else:
                canvas = panels([("live", frame)])
                canvas = add_banner(canvas, "NO PROFILE   [SPACE] save as reference   [q] quit", None)

            key = show(window, canvas)
            if key == 32:  # SPACE
                profile = save_reference(args, frame, fresh=reference is None)
                reference = profile.load_reference()
                rois = parse_rois(args.roi, (profile.width, profile.height)) or normalise_stored(
                    profile.roi, (profile.width, profile.height)
                )
            elif key in (ord("q"), 27):
                break
    finally:
        cap.release()
        cv2.destroyAllWindows()
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--camera", required=True, help="/dev/video0, a camera index, or an image file")
    ap.add_argument("--profile", required=True, help="profile name; created if it does not exist")
    ap.add_argument("--update", action="store_true", help="replace the golden frame with the current view")
    ap.add_argument(
        "--no-display",
        action="store_true",
        help="skip the window and just print a one-shot verdict (implied by --quiet/--json)",
    )
    ap.add_argument("--width", type=int, default=None)
    ap.add_argument("--height", type=int, default=None)
    ap.add_argument(
        "--roi",
        action="append",
        default=None,
        metavar="x,y,w,h",
        help="compare only this window (values <=1 are fractions). Repeat the flag, "
        "or separate regions with ';', to use several: --roi 0,0,0.3,0.4 --roi 0.7,0,0.3,0.4. "
        "Point them at scenery that never moves; leaving out the arm and the objects "
        "roughly halves the false-alarm rate. Stored in the profile when creating.",
    )
    # Thresholds resolve as: explicit flag > value stored in the profile > these.
    # The defaults are loose on purpose; tighten them per rig once you have measured
    # the real frame-to-frame variation (see --suggest).
    ap.add_argument("--max-shift", type=float, default=None, help="max translation in px")
    ap.add_argument("--min-correlation", type=float, default=None, help="min normalised correlation")
    ap.add_argument(
        "--suggest",
        type=int,
        default=0,
        metavar="N",
        help="sample N frames, report the observed spread, and propose thresholds. "
        "Move the arm and the objects around while it runs.",
    )
    ap.add_argument(
        "--suggest-roi",
        type=int,
        default=0,
        metavar="N",
        help="sample N frames and propose comparison windows automatically, by finding "
        "the areas that stay still while everything else moves. Move the robot and the "
        "objects while it runs.",
    )
    ap.add_argument(
        "--suggest-interval",
        type=float,
        default=2.0,
        help="seconds between --suggest samples, so you have time to move things",
    )
    ap.add_argument("--diff-out", default=None, help="write the overlay image here")
    ap.add_argument("--json", dest="as_json", action="store_true")
    ap.add_argument("--quiet", action="store_true", help="suppress output; rely on the exit code")
    ap.add_argument("--notes", default="")
    args = ap.parse_args()

    fresh = not Profile.exists(args.profile)
    resolve_thresholds(args, None if fresh else Profile.load(args.profile))
    # A still image cannot be shown as a live feed, and the scripted uses want a
    # verdict. --update is an explicit "save now" instruction, so it must not sit
    # behind a keypress in the window.
    headless = (
        args.no_display or args.quiet or args.as_json or args.update or is_image_path(args.camera)
    )

    if args.suggest_roi:
        return suggest_roi(args, args.suggest_roi)

    if args.suggest:
        if fresh:
            die(f"profile {args.profile!r} does not exist yet; create it first")
        profile = Profile.load(args.profile)
        rois = parse_rois(args.roi, (profile.width, profile.height)) or normalise_stored(
            profile.roi, (profile.width, profile.height)
        )
        return suggest_thresholds(args, profile, rois, args.suggest)

    if not headless:
        return run_window(args, None if fresh else Profile.load(args.profile))

    if fresh or args.update:
        frame = grab(args.camera, args.width, args.height)
        save_reference(args, frame, fresh)
        if fresh:
            print("\nThis view is now the golden reference. Re-run after every remount to verify.")
        return 0

    profile = Profile.load(args.profile)
    reference = profile.load_reference()
    # an explicit --roi overrides whatever the profile was created with
    rois = parse_rois(args.roi, (profile.width, profile.height)) or normalise_stored(
        profile.roi, (profile.width, profile.height)
    )
    frame = grab(args.camera, args.width, args.height)
    report = compare(reference, frame, rois)
    ok = evaluate(report, args)
    report["profile"] = profile.name
    report["ok"] = ok

    if args.diff_out:
        cv2.imwrite(args.diff_out, overlay(reference, frame, rois, report.get("regions")))
        if not args.quiet:
            print(f"wrote overlay to {args.diff_out}")

    if args.as_json:
        print(json.dumps(report, indent=2))
    elif not args.quiet:
        print_report(report, ok, profile.name, args)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
