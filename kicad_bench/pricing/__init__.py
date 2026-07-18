"""pricing — rough multi-vendor PCB cost estimation from project data.

See docs/PRICING_MODEL.md for the design + calibration. The `kb price` subcommand lives in
`cli.py` (exposes `add_parser`/`run` like every other tool module).
"""
from __future__ import annotations

from .cli import add_parser, run  # noqa: F401
