#!/usr/bin/env python
"""Live focus meter: turn the lens and watch the number peak.

    python rig_tools/rig_focus.py --camera /dev/video2

Put a TEXTURED target (printed text works well) at the distance the camera
actually has to resolve, then turn the lens until the reading stops rising.

Sharpness is scene-dependent -- a perfectly focused camera pointed at a blank
black table still scores low. Always measure against the same textured target,
and compare readings only within one session.

Keys: r resets the peak, s saves a snapshot, q/ESC quits.
"""

from __future__ import annotations

import argparse

import cv2
import numpy as np

from rig_common import add_banner, open_camera, panels, show


def sharpness(gray: np.ndarray) -> float:
    """Variance of the Laplacian -- the standard focus measure."""
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--camera", required=True, help="/dev/video2, or a camera index")
    ap.add_argument("--width", type=int, default=None)
    ap.add_argument("--height", type=int, default=None)
    ap.add_argument(
        "--centre",
        type=float,
        default=0.5,
        help="measure only this central fraction of the frame (1.0 = whole frame)",
    )
    ap.add_argument("--target", type=float, default=80.0, help="reading you are aiming for")
    ap.add_argument("--snapshot", default="focus.png", help="where 's' writes a snapshot")
    args = ap.parse_args()

    cap = open_camera(args.camera, args.width, args.height)
    peak, smoothed = 0.0, None
    print(f"focus meter on {args.camera} - turn the lens until the reading peaks.")
    print("Point it at a TEXTURED target at the real working distance, not a blank surface.")
    print("Keys: r reset peak, s snapshot, q quit")

    try:
        while True:
            grabbed, frame = cap.read()
            if not grabbed:
                continue
            h, w = frame.shape[:2]
            f = max(0.05, min(1.0, args.centre))
            cw, ch = int(w * f), int(h * f)
            x0, y0 = (w - cw) // 2, (h - ch) // 2
            patch = cv2.cvtColor(frame[y0 : y0 + ch, x0 : x0 + cw], cv2.COLOR_BGR2GRAY)

            value = sharpness(patch)
            # the raw measure is jumpy frame to frame; smooth it so the peak is findable
            smoothed = value if smoothed is None else 0.75 * smoothed + 0.25 * value
            peak = max(peak, smoothed)

            view = frame.copy()
            cv2.rectangle(view, (x0, y0), (x0 + cw, y0 + ch), (0, 255, 255), 1)
            edges = cv2.cvtColor(cv2.Laplacian(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), cv2.CV_8U), cv2.COLOR_GRAY2BGR)

            canvas = panels([("live (yellow box = measured)", view), ("edges", edges)])
            # a bar that fills as you approach the target
            frac = min(1.0, smoothed / max(args.target, 1e-6))
            bar_w = int(frac * (canvas.shape[1] - 20))
            cv2.rectangle(canvas, (10, canvas.shape[0] - 24), (10 + bar_w, canvas.shape[0] - 10), (0, 200, 255), -1)
            canvas = add_banner(
                canvas,
                f"sharpness {smoothed:7.1f}   peak {peak:7.1f}   target {args.target:.0f}"
                f"   {'REACHED' if smoothed >= args.target else 'keep turning'}"
                "   [r] reset  [s] save  [q] quit",
                smoothed >= args.target,
            )

            key = show(f"rig_focus [{args.camera}]", canvas)
            if key == ord("r"):
                peak, smoothed = 0.0, None
            elif key == ord("s"):
                cv2.imwrite(args.snapshot, frame)
                print(f"saved {args.snapshot} at sharpness {smoothed:.1f}")
            elif key in (ord("q"), 27):
                break
    finally:
        cap.release()
        cv2.destroyAllWindows()

    print(f"peak sharpness reached: {peak:.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
