#!/usr/bin/env python3
"""Exercise the e-paper panel: full refresh test pattern, then partial refreshes.

Usage: check_display.py [-c config.toml] [--rev V4] [--png out.png]
--png renders to a file instead of the panel (desktop test, no hardware).
Check on the panel: border intact, "TOP-LEFT" upper left, no stuck/ghost pixels.
"""
import argparse
import sys
import time

import sb_eink as sb


def pattern(cfg):
    from PIL import Image, ImageDraw

    img = Image.new("1", (sb.W, sb.H), 1)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, sb.W - 1, sb.H - 1], outline=0)
    d.line([(0, 0), (sb.W - 1, sb.H - 1)], fill=0)
    d.line([(0, sb.H - 1), (sb.W - 1, 0)], fill=0)
    d.rectangle([sb.W // 2 - 40, sb.H // 2 - 14, sb.W // 2 + 40, sb.H // 2 + 14], fill=1, outline=0)
    font = sb.load_font(cfg["font_path"], 12)
    d.text((4, 3), "TOP-LEFT", font=font, fill=0)
    d.text((sb.W - 4 - d.textlength("BOTTOM-RIGHT", font=font), sb.H - 16), "BOTTOM-RIGHT", font=font, fill=0)
    d.text((sb.W // 2 - 32, sb.H // 2 - 7), "%dx%d" % (sb.W, sb.H), font=font, fill=0)
    return img.rotate(180) if cfg["rotate180"] else img


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-c", "--config", help="TOML config (revision, rotate180, font_path)")
    ap.add_argument("--rev", help="override epd_revision (V2, V3, V4, b_V4)")
    ap.add_argument("--png", help="write PNG instead of using the panel")
    ap.add_argument("--delay", type=float, default=3.0, help="seconds between steps")
    args = ap.parse_args(argv)

    try:
        cfg = sb.load_config(args.config)
        if args.rev:
            cfg["epd_revision"] = args.rev
        sb.validate_config(cfg, need_panel=not args.png, need_server=False)
        panel = sb.PngPanel(args.png) if args.png else sb.EpdPanel(cfg["epd_revision"])
    except sb.SBError as e:
        print("FAIL: %s" % e)
        return 1

    steps = [("full refresh: test pattern", pattern(cfg), True)]
    for n in range(1, 4):
        task = sb.Task("x", "Partial refresh test %d of 3" % n)
        steps.append(("partial refresh %d" % n, sb.render(cfg, task, n - 1, 3, False, ""), False))
    steps.append(("full refresh: offline marker", sb.render(cfg, None, 0, 0, True, "Display test done"), True))

    try:
        for i, (label, img, full) in enumerate(steps):
            print("[%d/%d] %s" % (i + 1, len(steps), label), flush=True)
            start = time.monotonic()
            panel.show(img, full)
            print("      took %.1fs" % (time.monotonic() - start))
            if i < len(steps) - 1:
                time.sleep(args.delay)
    except Exception as e:  # driver/SPI errors surface here
        print("FAIL: %s: %s" % (type(e).__name__, e))
        print("  check: SPI enabled (raspi-config), HAT seated, epd_revision matches the board, user in spi/gpio groups")
        return 1
    print("OK: sequence completed; verify the panel visually")
    return 0


if __name__ == "__main__":
    sys.exit(main())
