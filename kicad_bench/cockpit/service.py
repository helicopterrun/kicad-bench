"""State and serialization helpers for the optional Cockpit web app."""
from __future__ import annotations

import subprocess
import secrets
import threading
from dataclasses import asdict
from pathlib import Path

from .. import datasheet as dsmod
from .. import stage
from ..board_state import BoardState
from ..core import config as cfgmod
from ..layout import release_prep


class CockpitService:
    """Own one independent ``BoardState`` per board in a product config."""

    def __init__(self, config_path: Path):
        self.config_path = Path(config_path).resolve()
        self.mutation_token = secrets.token_urlsafe(32)
        base = cfgmod.load(self.config_path)
        self.base = base
        self.states: dict[str, BoardState] = {}
        for board in base.boards:
            cfg = cfgmod.load(self.config_path)
            cfg.activate(board.name)
            self.states[board.name] = BoardState(cfg)
        self._release: dict[str, dict] = {}
        self._release_running: set[str] = set()
        self._lock = threading.Lock()

    def state(self, board: str) -> BoardState:
        try:
            return self.states[board]
        except KeyError as exc:
            raise ValueError(f"unknown board: {board}") from exc

    def product(self) -> dict:
        p = self.base.product
        return {
            "name": p.name if p else self.base.root.name,
            "root": str(self.base.root),
            "config": str(self.config_path),
            "boards": len(self.states),
            "mutation_token": self.mutation_token,
        }

    def boards(self) -> list[dict]:
        rows = []
        for name, state in self.states.items():
            cfg = state.cfg
            rows.append({
                "id": name,
                "name": name,
                "schematic": cfg.root_sch.name if cfg.root_sch else None,
                "pcb": cfg.pcb.name if cfg.pcb else None,
                "has_schematic": bool(cfg.root_sch and cfg.root_sch.exists()),
                "has_pcb": bool(cfg.pcb and cfg.pcb.exists()),
                "pcb_mtime": state._mtime(),
                "sch_mtime": state.sch_mtime(),
            })
        return rows

    @staticmethod
    def git_status(root: Path) -> dict:
        def run(*args):
            return subprocess.run(
                ["git", *args], cwd=root, capture_output=True, text=True, timeout=10
            )

        try:
            branch = run("branch", "--show-current").stdout.strip()
            dirty = run("status", "--porcelain").stdout.splitlines()
            head = run("log", "-1", "--pretty=%h%x00%s%x00%cr").stdout.strip().split("\0")
            return {
                "branch": branch or "detached",
                "dirty": len(dirty),
                "head": head[0] if head else "",
                "subject": head[1] if len(head) > 1 else "",
                "when": head[2] if len(head) > 2 else "",
            }
        except (OSError, subprocess.SubprocessError):
            return {"branch": "", "dirty": 0, "head": "", "subject": "", "when": ""}

    @staticmethod
    def stage_status(state: BoardState) -> dict:
        sp = stage.paths_from_config(state.cfg)
        jobs = stage.queued_jobs(sp)
        hooks = stage.hooks_list(sp)
        last = stage._load_last_run(sp)
        locks = stage.locks_present(sp)
        return {
            "open": stage.project_open(sp),
            "locks": locks,
            "jobs": jobs,
            "hooks": hooks,
            "last_run": last,
        }

    def status(self, board: str) -> dict:
        state = self.state(board)
        return {
            "board": board,
            "pcb_mtime": state._mtime(),
            "sch_mtime": state.sch_mtime(),
            "audit": state.audit_peek(),
            "review": state.review_peek(),
            "stage": self.stage_status(state),
            "git": self.git_status(state.cfg.root),
            "release": self.release_peek(board),
        }

    def release_peek(self, board: str) -> dict:
        with self._lock:
            if board in self._release_running:
                return {"status": "running"}
            return self._release.get(board, {"status": "none"})

    def release_check(self, board: str) -> dict:
        self.state(board)
        with self._lock:
            if board in self._release_running:
                return {"status": "running"}
            self._release_running.add(board)
        threading.Thread(target=self._release_bg, args=(board,), daemon=True).start()
        return {"status": "running"}

    def _release_bg(self, board: str) -> None:
        state = self.state(board)
        try:
            ready, result, laid_out = release_prep.readiness(state.cfg)
            data = {
                "status": "ready",
                "ready": ready,
                "laid_out": laid_out,
                "summary": result.summary,
                "findings": [asdict(f) for f in result.findings],
            }
        except BaseException as exc:  # SystemExit is how legacy setup errors surface
            data = {"status": "error", "error": str(exc)}
        with self._lock:
            self._release[board] = data
            self._release_running.discard(board)

    @staticmethod
    def datasheets(state: BoardState) -> dict:
        return {
            "items": [
                {"i": i, "name": str(dsmod._rel(path, state.cfg.root))}
                for i, path in enumerate(state.datasheet_list())
            ]
        }
