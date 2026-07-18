"""Optional FastAPI/Vue cockpit command.

Imports stay lazy so every core ``kb`` command still works without the ``web`` extra.
"""
from __future__ import annotations

from ..core import config as cfgmod


def run(args) -> int:
    try:
        import uvicorn
        from .app import create_app
    except ImportError as exc:
        raise SystemExit(
            "error: kb cockpit needs the web extra; install with "
            "`pip install -e '.[web]'`"
        ) from exc

    cfg_path = cfgmod.resolve_config_path(args.config)
    if not cfg_path or not cfg_path.exists():
        raise SystemExit(
            f"error: no {cfgmod.CONFIG_NAME} found; pass --config or run from a project"
        )
    loopback = args.host in {"127.0.0.1", "localhost", "::1"}
    if not loopback and not args.allow_network:
        raise SystemExit(
            "error: refusing a network bind because the cockpit can run staged commands; "
            "use --allow-network only on a trusted network"
        )
    if not loopback:
        print("warning: cockpit is reachable from the network and can run staged commands")
    print(f"KiCad cockpit -> http://{args.host}:{args.port}   (Ctrl-C to stop)")
    uvicorn.run(create_app(cfg_path), host=args.host, port=args.port, log_level="warning")
    return 0


def add_parser(sub) -> None:
    p = sub.add_parser(
        "cockpit", help="multi-board web workspace for audit, previews, parts, and release"
    )
    p.add_argument("--host", default="127.0.0.1", help="bind host (default 127.0.0.1)")
    p.add_argument("--port", type=int, default=8766, help="port (default 8766)")
    p.add_argument(
        "--allow-network", action="store_true",
        help="allow a non-loopback bind (trusted networks only; staged commands are exposed)",
    )
    p.add_argument("--config", help=f"path to {cfgmod.CONFIG_NAME}")
    p.set_defaults(func=run)
