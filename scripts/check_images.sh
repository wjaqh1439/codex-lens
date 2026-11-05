#!/bin/sh
set -eu
python3 -m unittest discover -s tests -v
python3 scripts/browser_check.py --images-only
