#!/usr/bin/env python
"""ArUco viewpoint calibration: pin a camera pose down so it survives disassembly.

    # first time (or --update): the current view becomes the golden reference
    python rig_tools/rig_calib.py --camera /dev/video0 --profile top

    # every session after: verify, and get the homography that cancels any drift
    python rig_tools/rig_calib.py --camera /dev/video0 --profile top

Running it opens a window. If the profile already exists you get the saved
reference next to the live view with a drift readout; if it does not, you get the
live view alone with the marker count, and SPACE saves it as the reference.

Exit code is 0 when the view is within tolerance and 1 when it is not, so it can
gate a recording script:

    python rig_tools/rig_calib.py --camera /dev/video0 --profile top --quiet || exit 1

Because the markers lie flat on the table, the homography is exact for the table
plane. Feed it to ``rig_common.warp_to_reference`` in the recording pipeline and
every session lands on the same viewpoint regardless of how the mount was reseated.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json

import cv2
import numpy as np

from rig_common import (
    Profile,
    add_banner,
    describe_homography,
    detect_markers,
    die,
    draw_markers,
    grab,
    homography_to_reference,
    is_image_path,
    open_camera,
    panels,
    show,
    warp_to_reference,
)


def create_profile(args, frame: np.ndarray) -> Profile:
    markers = detect_markers(frame, args.dict)
    if len(markers) < args.min_markers:
        die(
            f"only {len(markers)} marker(s) detected, need >={args.min_markers}. "
            "Check lighting, that all markers are inside the frame, and that they are flat."
        )
    h, w = frame.shape[:2]
    profile = Profile(
        name=args.profile,
        camera=args.camera,
        width=w,
        height=h,
        aruco_dict=args.dict,
        ref_corners={str(k): v.tolist() for k, v in markers.items()},
        created=dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        notes=args.notes,
    )
    profile.save(reference=frame)
    cv2.imwrite(str(profile.dir / "reference_markers.png"), draw_markers(frame, markers))
    print(f"created profile {profile.name!r} at {profile.dir}")
    print(f"  frame      : {w}x{h}")
    print(f"  markers    : {sorted(markers)} ({args.dict})")
    print("  saved      : profile.json, reference.png, reference_markers.png")
    print("\nThis view is now the golden reference. Re-run after every remount to verify.")
    return profile


def verify(profile: Profile, frame: np.ndarray, args) -> tuple[bool, dict]:
    markers = detect_markers(frame, profile.aruco_dict)
    ref = profile.corners_as_arrays()
    H, shared, rms = homography_to_reference(markers, ref)

    report: dict[str, object] = {
        "profile": profile.name,
        "detected": sorted(markers),
        "reference": sorted(ref),
        "matched": shared,
    }
    if H is None:
        report |= {"ok": False, "reason": f"matched only {len(shared)} marker(s), need >=2"}
        return False, report

    stats = describe_homography(H, (profile.width, profile.height))
    report |= stats
    report["reprojection_rms_px"] = rms
    report["homography"] = H.tolist()

    ok = (
        len(shared) >= args.min_markers
        and stats["max_corner_shift_px"] <= args.max_shift
        and abs(stats["rotation_deg"]) <= args.max_rotation
        and abs(stats["scale"] - 1.0) <= args.max_scale
    )
    report["ok"] = ok
    if not ok and len(shared) < args.min_markers:
        report["reason"] = f"matched {len(shared)} marker(s), need >={args.min_markers}"
    return ok, report


def print_report(report: dict, args) -> None:
    if report.get("ok"):
        print(f"OK   view matches profile {report['profile']!r}")
    else:
        print(f"FAIL view drifted from profile {report['profile']!r}")
        if "reason" in report:
            print(f"     {report['reason']}")
    print(f"     markers matched : {report['matched']} (detected {report['detected']})")
    if "max_corner_shift_px" in report:
        print(
            f"     shift           : dx {report['dx_px']:+.1f} px, dy {report['dy_px']:+.1f} px, "
            f"max corner {report['max_corner_shift_px']:.1f} px (limit {args.max_shift:.1f})"
        )
        print(
            f"     rotation/scale  : {report['rotation_deg']:+.2f} deg (limit {args.max_rotation:.2f}), "
            f"x{report['scale']:.4f} (limit +/-{args.max_scale:.3f})"
        )
        print(f"     fit residual    : {report['reprojection_rms_px']:.2f} px rms")
    if not report.get("ok"):
        print("\n     Nudge the mount and re-run; the window view gives continuous feedback.")
        print("     If the mount cannot be recovered, --update re-anchors the profile,")
        print("     but then previously recorded episodes no longer share this viewpoint.")


def run_window(args, profile: Profile | None) -> int:
    """Show the saved reference alongside the live view, or the live view alone."""
    cap = open_camera(args.camera, args.width, args.height)
    window = f"rig_calib [{args.profile}]"
    if profile:
        reference = draw_markers(profile.load_reference(), profile.corners_as_arrays(), (0, 200, 255))
        print("reference | live - adjust the mount until the banner turns green.")
        print("SPACE = re-anchor to the current view, q or ESC = quit")
    else:
        reference = None
        print(f"no profile named {args.profile!r} yet - showing the live view only")
        print("Frame the whole pick area, get all 4 markers in shot, then SPACE to save.")

    ok = False
    try:
        while True:
            grabbed, frame = cap.read()
            if not grabbed:
                continue

            if profile is not None:
                ok, report = verify(profile, frame, args)
                markers = detect_markers(frame, profile.aruco_dict)
                live = draw_markers(frame, markers, (0, 255, 0) if ok else (0, 0, 255))
                status = f"{'OK' if ok else 'ADJUST'}   matched {len(report['matched'])}/{len(profile.ref_corners)}"
                if "max_corner_shift_px" in report:
                    status += (
                        f"   dx{report['dx_px']:+.0f} dy{report['dy_px']:+.0f}"
                        f"   max {report['max_corner_shift_px']:.1f}px (limit {args.max_shift:.0f})"
                        f"   rot {report['rotation_deg']:+.2f}deg"
                    )
                status += "   [SPACE] re-anchor  [q] quit"
                canvas = add_banner(panels([("reference", reference), ("live", live)]), status, ok)
            else:
                markers = detect_markers(frame, args.dict)
                live = draw_markers(frame, markers, (0, 255, 0) if len(markers) >= args.min_markers else (0, 0, 255))
                canvas = add_banner(
                    panels([("live", live)]),
                    f"NO PROFILE   markers detected: {sorted(markers)}"
                    f"   need >={args.min_markers}   [SPACE] save   [q] quit",
                    None,
                )

            key = show(window, canvas)
            if key == 32:  # SPACE
                profile = create_profile(args, frame)
                reference = draw_markers(profile.load_reference(), profile.corners_as_arrays(), (0, 200, 255))
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
    ap.add_argument("--update", action="store_true", help="re-anchor an existing profile to the current view")
    ap.add_argument(
        "--no-display",
        action="store_true",
        help="skip the window and just print a one-shot verdict (implied by --quiet/--json)",
    )
    ap.add_argument("--dict", default="4x4_50", help="marker family (only used when creating)")
    ap.add_argument("--width", type=int, default=None, help="request this capture width")
    ap.add_argument("--height", type=int, default=None, help="request this capture height")
    ap.add_argument("--min-markers", type=int, default=3, help="markers that must match to pass")
    ap.add_argument("--max-shift", type=float, default=4.0, help="max corner shift in px")
    ap.add_argument("--max-rotation", type=float, default=1.0, help="max rotation in degrees")
    ap.add_argument("--max-scale", type=float, default=0.02, help="max fractional scale change")
    ap.add_argument("--warp-out", default=None, help="write the drift-corrected frame here")
    ap.add_argument("--json", dest="as_json", action="store_true", help="print the report as JSON")
    ap.add_argument("--quiet", action="store_true", help="suppress output; rely on the exit code")
    ap.add_argument("--notes", default="", help="free-text note stored in the profile")
    args = ap.parse_args()

    fresh = not Profile.exists(args.profile)
    # A still image cannot be shown as a live feed, and the scripted uses want a verdict.
    headless = args.no_display or args.quiet or args.as_json or is_image_path(args.camera)

    if not headless:
        return run_window(args, None if fresh else Profile.load(args.profile))

    if fresh or args.update:
        frame = grab(args.camera, args.width, args.height)
        create_profile(args, frame)
        if args.update and not fresh:
            print("\nnote: profile re-anchored; earlier episodes were recorded from the OLD view.")
        return 0

    profile = Profile.load(args.profile)
    frame = grab(args.camera, args.width, args.height)
    if (frame.shape[1], frame.shape[0]) != (profile.width, profile.height):
        print(
            f"warning: frame is {frame.shape[1]}x{frame.shape[0]} but profile was made at "
            f"{profile.width}x{profile.height}; thresholds are in profile pixels"
        )
    ok, report = verify(profile, frame, args)

    if args.warp_out and report.get("homography"):
        H = np.asarray(report["homography"], dtype=np.float64)
        cv2.imwrite(args.warp_out, warp_to_reference(frame, H, (profile.width, profile.height)))
        if not args.quiet:
            print(f"wrote drift-corrected frame to {args.warp_out}")

    if args.as_json:
        print(json.dumps(report, indent=2))
    elif not args.quiet:
        print_report(report, args)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
