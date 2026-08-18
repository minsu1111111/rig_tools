#!/usr/bin/env python
"""Generate a printable ArUco sheet for the rig.

    python rig_tools/make_markers.py --size-mm 70 --out rig_tools/markers.png

Print at 100% scale (no "fit to page"), on MATTE paper. Glossy stock catches the
rig's overhead light and the blown highlight destroys detection.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import cv2
import numpy as np

from rig_common import ARUCO_DICTS, die

MM_PER_INCH = 25.4


def render_marker(dict_name: str, marker_id: int, px: int) -> np.ndarray:
    dictionary = cv2.aruco.getPredefinedDictionary(ARUCO_DICTS[dict_name])
    return cv2.aruco.generateImageMarker(dictionary, marker_id, px)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dict", default="4x4_50", choices=sorted(ARUCO_DICTS), help="marker family")
    ap.add_argument("--ids", type=int, nargs="+", default=[0, 1, 2, 3], help="marker ids to print")
    ap.add_argument("--size-mm", type=float, default=70.0, help="black square edge length in mm")
    ap.add_argument("--dpi", type=int, default=300)
    ap.add_argument("--cols", type=int, default=2)
    ap.add_argument("--out", default="rig_tools/markers.png")
    args = ap.parse_args()

    if args.size_mm < 40:
        print(f"warning: {args.size_mm:.0f}mm markers are small for a 320x240 stream; 60-80mm is safer")

    side = int(round(args.size_mm / MM_PER_INCH * args.dpi))
    quiet = max(1, side // 6)  # ArUco needs a white quiet zone of >=1 module
    label_h = int(0.35 * quiet) + 12
    cell_w, cell_h = side + 2 * quiet, side + 2 * quiet + label_h

    cols = max(1, args.cols)
    rows = (len(args.ids) + cols - 1) // cols
    sheet = np.full((rows * cell_h, cols * cell_w), 255, dtype=np.uint8)

    for k, mid in enumerate(args.ids):
        r, c = divmod(k, cols)
        y0, x0 = r * cell_h, c * cell_w
        sheet[y0 + quiet : y0 + quiet + side, x0 + quiet : x0 + quiet + side] = render_marker(
            args.dict, mid, side
        )
        # cut guides + a human-readable label so you can tell them apart on the table
        cv2.rectangle(sheet, (x0 + 2, y0 + 2), (x0 + cell_w - 3, y0 + cell_h - label_h - 3), 200, 1)
        cv2.putText(
            sheet,
            f"{args.dict}  id={mid}  {args.size_mm:.0f}mm",
            (x0 + quiet, y0 + cell_h - 14),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5,
            0,
            1,
            cv2.LINE_AA,
        )

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(out), sheet):
        die(f"failed to write {out}")

    sheet_mm = (cols * cell_w / args.dpi * MM_PER_INCH, rows * cell_h / args.dpi * MM_PER_INCH)
    print(f"wrote {out}  ({sheet.shape[1]}x{sheet.shape[0]} px @ {args.dpi} dpi)")
    print(f"sheet size: {sheet_mm[0]:.0f} x {sheet_mm[1]:.0f} mm  -> print at 100% scale, MATTE paper")
    print(f"markers: {args.dict} ids {args.ids}, black square {args.size_mm:.0f}mm each")


if __name__ == "__main__":
    main()
