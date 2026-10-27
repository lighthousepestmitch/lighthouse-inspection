#!/usr/bin/env python3
"""Regenerate index.html (the live Inspection Capture site) from the editable source.

Source of truth: ~/business-context/termite-inspection-capture.html (no html/head/body wrapper).
This wraps it: head = everything up to and including </style>, body = the rest.
Usage: python3 ~/lighthouse-inspection/tools/build_capture.py
Then: git -C ~/lighthouse-inspection commit, and Mitch runs the push.
"""
import pathlib

SRC = pathlib.Path.home() / "business-context" / "termite-inspection-capture.html"
OUT = pathlib.Path(__file__).resolve().parent.parent / "index.html"

src = SRC.read_text(encoding="utf-8")
cut = src.index("</style>") + len("</style>")
head, body = src[:cut], src[cut:].lstrip("\n")
html = (
    '<!DOCTYPE html>\n<html lang="en-AU">\n<head>\n<meta charset="utf-8">\n'
    + head
    + "\n</head>\n<body>\n"
    + body.rstrip("\n")
    + "\n\n</body>\n</html>\n"
)
OUT.write_text(html, encoding="utf-8")
print(f"wrote {OUT} ({len(html.splitlines())} lines) from {SRC}")
