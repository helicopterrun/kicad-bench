"""stage.py — `kb stage`: apply queued changes the moment KiCad is safe to touch.

The problem this solves
-----------------------
When Claude edits the same working tree the user opens in KiCad on one machine, a
generated `.kicad_sch` (or anything touching the top-level sheet / the `.kicad_pcb`)
must NOT be written while KiCad holds the project open, or KiCad clobbers it / fights
the lock. The hand-off used to be manual: "KiCad's closed, run the script."

This daemon automates that hand-off. Claude *stages* a job while KiCad is still open;
the daemon watches the KiCad lock state and, the instant the project is closed and
stable, runs the staged jobs, pops a desktop notification, and publishes a live status
page the sidecar shows as a tab.

"KiCad has THIS project open" is detected precisely:
    project_open  ==  (~*.lck lock files exist in the project's lock dir)
                      AND (a `kicad`/`eeschema`/`pcbnew` process is alive)
The `.lck` files are project-specific (KiCad writes one per open file); requiring a
live process too means a crash that leaves stale locks still counts as *closed*, so
jobs aren't stranded forever.

Generalized from the Example Board project's `scripts/stage_runner.py`: paths come
from the project's `kicad-bench.toml` (root + the lock dir = the KiCad project dir,
i.e. `root_sch`'s parent) instead of being hard-coded, and the desktop notifier works
on macOS (osascript) or Linux (notify-send). State lives under `<project>/.stage/`
(gitignored). This is the LOCAL counterpart to the git-branch lock used when Claude
runs headless on a remote server.

CLI
---
    kb stage add --label "Rebuild touch sheet" -- python3 scripts/build_touch_sheet.py
    kb stage status      # one-line state (open/closed, N queued)
    kb stage list        # show queue + last run
    kb stage clear       # drop all queued jobs
    kb stage run-now     # force-drain NOW (manual override; ignores lock)
    kb stage daemon      # the watch loop (run in background / via the daemon unit)

    # RECURRING hooks — run once on EVERY KiCad-close (vs queue jobs, one-shot):
    kb stage hook-add --label "refresh pour report" -- python3 scripts/build_pour_report.py
    kb stage hook-list
    kb stage hook-rm <id|all>
    kb stage hook-run    # run all hooks now (manual override)

The daemon is a singleton (pidfile). Start it once and leave it running beside the
sidecar; it survives across Claude sessions.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .core import config as cfgmod

GRACE_SEC = 4.0        # KiCad must be closed-and-stable this long before we run
POLL_SEC = 2.0
OUTPUT_CAP = 4000      # chars of captured job output to retain


# ── per-project paths (from the kicad-bench.toml) ────────────────────────────
@dataclass
class StagePaths:
    """All filesystem locations the stage-runner uses, derived from the config.

    `root` is the design-repo root; `lock_dir` is the KiCad project directory where
    KiCad writes its `~*.lck` files (the parent of the root schematic / PCB).
    """
    root: Path
    lock_dir: Path
    project_name: str

    @property
    def stage(self) -> Path: return self.root / ".stage"
    @property
    def queue(self) -> Path: return self.stage / "queue"
    @property
    def done(self) -> Path: return self.stage / "done"
    @property
    def hooks(self) -> Path: return self.stage / "hooks"
    @property
    def status_html(self) -> Path: return self.stage / "status.html"
    @property
    def last_run(self) -> Path: return self.stage / "last_run.json"
    @property
    def hooks_last(self) -> Path: return self.stage / "hooks_last.json"
    @property
    def pidfile(self) -> Path: return self.stage / "daemon.pid"

    def ensure_dirs(self) -> None:
        for d in (self.stage, self.queue, self.done, self.hooks):
            d.mkdir(parents=True, exist_ok=True)


def paths_from_config(cfg) -> StagePaths:
    """KiCad writes its lock files next to the project file, so the lock dir is the
    parent of the root schematic (preferred) or the PCB; fall back to the repo root."""
    if cfg.root_sch:
        lock_dir = cfg.root_sch.parent
    elif cfg.pcb:
        lock_dir = cfg.pcb.parent
    else:
        lock_dir = cfg.root
    return StagePaths(root=cfg.root, lock_dir=lock_dir, project_name=cfg.root.name)


# ── KiCad open/closed detection ─────────────────────────────────────────────
def locks_present(sp: StagePaths) -> list[str]:
    return [Path(p).name for p in glob.glob(str(sp.lock_dir / "~*.lck"))]


def kicad_running() -> bool:
    """True if a KiCad GUI process is alive. Uses pgrep (present on macOS and Linux);
    if pgrep is unavailable we cannot tell, so we report False and fall back to
    lock-file-only detection in `project_open` (conservative: stale locks alone keep a
    job staged, recoverable via `run-now`)."""
    pgrep = shutil.which("pgrep")
    if not pgrep:
        return False
    for name in ("kicad", "eeschema", "pcbnew"):
        try:
            if subprocess.run([pgrep, "-x", name], capture_output=True).returncode == 0:
                return True
        except OSError:
            pass
    return False


def project_open(sp: StagePaths) -> bool:
    """True iff THIS project is held open in KiCad. If we can detect a live KiCad
    process, require both locks AND a process (crash-safe). If process detection is
    unavailable (no pgrep), fall back to locks alone."""
    locks = bool(locks_present(sp))
    if not locks:
        return False
    return kicad_running() if shutil.which("pgrep") else True


# ── queue ───────────────────────────────────────────────────────────────────
def add_job(sp: StagePaths, label: str, cmd: list[str]) -> Path:
    sp.ensure_dirs()
    seq = len(glob.glob(str(sp.queue / "*.json")))
    job = {
        "id": f"{int(time.time())}-{seq:02d}",
        "label": label,
        "cmd": cmd,
        "cwd": str(sp.root),
        "added": datetime.now().isoformat(timespec="seconds"),
    }
    path = sp.queue / f"{job['id']}.json"
    path.write_text(json.dumps(job, indent=2))
    return path


def queued_jobs(sp: StagePaths) -> list[dict]:
    out = []
    for p in sorted(glob.glob(str(sp.queue / "*.json"))):
        try:
            out.append(json.loads(Path(p).read_text()))
        except json.JSONDecodeError:
            pass
    return out


def clear_queue(sp: StagePaths) -> int:
    jobs = glob.glob(str(sp.queue / "*.json"))
    for p in jobs:
        Path(p).unlink()
    return len(jobs)


def _save_last_run(sp: StagePaths, results: list[dict]) -> None:
    sp.last_run.write_text(json.dumps({
        "finished": datetime.now().isoformat(timespec="seconds"),
        "results": results,
    }, indent=2))


def _load_last_run(sp: StagePaths) -> dict | None:
    if sp.last_run.exists():
        try:
            return json.loads(sp.last_run.read_text())
        except json.JSONDecodeError:
            return None
    return None


def drain_queue(sp: StagePaths) -> list[dict]:
    """Run every queued job in order; move each to done/. Returns result dicts."""
    sp.ensure_dirs()
    results = []
    for p in sorted(glob.glob(str(sp.queue / "*.json"))):
        job = json.loads(Path(p).read_text())
        started = datetime.now().isoformat(timespec="seconds")
        try:
            cp = subprocess.run(job["cmd"], cwd=job.get("cwd", str(sp.root)),
                                capture_output=True, text=True, timeout=600)
            out = (cp.stdout + ("\n" + cp.stderr if cp.stderr else ""))[-OUTPUT_CAP:]
            res = {"label": job["label"], "cmd": job["cmd"], "exit": cp.returncode,
                   "ok": cp.returncode == 0, "started": started, "output": out.strip()}
        except subprocess.TimeoutExpired:
            res = {"label": job["label"], "cmd": job["cmd"], "exit": -1,
                   "ok": False, "started": started, "output": "TIMEOUT after 600s"}
        except Exception as e:  # noqa: BLE001 — surface any launch failure as a result
            res = {"label": job["label"], "cmd": job["cmd"], "exit": -1,
                   "ok": False, "started": started, "output": f"launch error: {e}"}
        results.append(res)
        # archive the job file (with its result) out of the live queue
        job["result"] = res
        (sp.done / Path(p).name).write_text(json.dumps(job, indent=2))
        Path(p).unlink()
    if results:
        _save_last_run(sp, results)
    return results


# ── hooks (recurring: run once on every KiCad-close) ────────────────────────
def hook_add(sp: StagePaths, label: str, cmd: list[str]) -> Path:
    sp.ensure_dirs()
    seq = len(glob.glob(str(sp.hooks / "*.json")))
    hook = {"id": f"{int(time.time())}-{seq:02d}", "label": label,
            "cmd": cmd, "cwd": str(sp.root),
            "added": datetime.now().isoformat(timespec="seconds")}
    path = sp.hooks / f"{hook['id']}.json"
    path.write_text(json.dumps(hook, indent=2))
    return path


def hooks_list(sp: StagePaths) -> list[dict]:
    out = []
    for p in sorted(glob.glob(str(sp.hooks / "*.json"))):
        try:
            h = json.loads(Path(p).read_text())
            h["_path"] = p
            out.append(h)
        except json.JSONDecodeError:
            pass
    return out


def hook_clear(sp: StagePaths, which: str = "all") -> int:
    hooks = glob.glob(str(sp.hooks / "*.json"))
    n = 0
    for p in hooks:
        if which == "all" or Path(p).stem == which or which in Path(p).read_text():
            Path(p).unlink()
            n += 1
    return n


def run_hooks(sp: StagePaths) -> list[dict]:
    """Run every registered hook once (called on each KiCad-close episode).
    Hooks persist — they are NOT consumed like queue jobs."""
    results = []
    for h in hooks_list(sp):
        started = datetime.now().isoformat(timespec="seconds")
        try:
            cp = subprocess.run(h["cmd"], cwd=h.get("cwd", str(sp.root)),
                                capture_output=True, text=True, timeout=600)
            out = (cp.stdout + ("\n" + cp.stderr if cp.stderr else ""))[-OUTPUT_CAP:]
            results.append({"label": h["label"], "exit": cp.returncode,
                            "ok": cp.returncode == 0, "started": started,
                            "output": out.strip()})
        except Exception as e:  # noqa: BLE001
            results.append({"label": h["label"], "exit": -1, "ok": False,
                            "started": started, "output": f"error: {e}"})
    if results:
        sp.hooks_last.write_text(json.dumps(
            {"finished": datetime.now().isoformat(timespec="seconds"),
             "results": results}, indent=2))
    return results


def _load_hooks_last(sp: StagePaths) -> dict | None:
    if sp.hooks_last.exists():
        try:
            return json.loads(sp.hooks_last.read_text())
        except json.JSONDecodeError:
            return None
    return None


# ── notification (macOS osascript / Linux notify-send / silent) ──────────────
def notify(title: str, message: str, sound: str = "Glass") -> None:
    osa = shutil.which("osascript")
    if osa:
        script = (f'display notification {json.dumps(message)} '
                  f'with title {json.dumps(title)} sound name {json.dumps(sound)}')
        try:
            subprocess.run([osa, "-e", script], capture_output=True, timeout=10)
        except Exception:  # noqa: BLE001 — a notifier must never crash the daemon
            pass
        return
    nsend = shutil.which("notify-send")
    if nsend:
        try:
            subprocess.run([nsend, title, message], capture_output=True, timeout=10)
        except Exception:  # noqa: BLE001
            pass


# ── live status page (served by the sidecar as a tab) ───────────────────────
def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def write_status(sp: StagePaths, open_now: bool, locks: list[str],
                 pending_run: bool = False) -> None:
    sp.ensure_dirs()
    jobs = queued_jobs(sp)
    last = _load_last_run(sp)
    hooks = hooks_list(sp)
    hooks_last = _load_hooks_last(sp)
    now = datetime.now().strftime("%H:%M:%S")

    if open_now:
        state_cls, state_txt = "open", "KiCad OPEN — staged jobs held"
    elif pending_run:
        state_cls, state_txt = "run", "running staged jobs…"
    else:
        state_cls, state_txt = "closed", "KiCad CLOSED — safe to apply"

    rows = []
    if jobs:
        for j in jobs:
            rows.append(f'<div class="job"><span class="lbl">{_esc(j["label"])}</span>'
                        f'<span class="cmd">{_esc(" ".join(j["cmd"]))}</span>'
                        f'<span class="age">added {_esc(j["added"][11:])}</span></div>')
    else:
        rows.append('<div class="empty">queue empty</div>')

    last_html = ""
    if last:
        items = []
        for r in last["results"]:
            cls = "ok" if r["ok"] else "fail"
            badge = "OK" if r["ok"] else f"FAIL ({r['exit']})"
            out = _esc(r["output"])[:1500]
            items.append(
                f'<div class="res {cls}"><div class="resh"><span class="rbadge">{badge}</span>'
                f'<span class="lbl">{_esc(r["label"])}</span></div>'
                + (f'<pre>{out}</pre>' if out else "") + '</div>')
        last_html = (f'<h2>Last run · {_esc(last["finished"][11:])}</h2>'
                     + "".join(items))

    hooks_html = ""
    if hooks:
        hrows = "".join(f'<div class="job"><span class="lbl">↻ {_esc(h["label"])}</span>'
                        f'<span class="cmd">{_esc(" ".join(h["cmd"]))}</span></div>'
                        for h in hooks)
        hl = ""
        if hooks_last:
            hi = "".join(
                f'<div class="res {"ok" if r["ok"] else "fail"}"><div class="resh">'
                f'<span class="rbadge">{"OK" if r["ok"] else f"FAIL ({r['exit']})"}</span>'
                f'<span class="lbl">{_esc(r["label"])}</span></div></div>'
                for r in hooks_last["results"])
            hl = (f'<div class="age">ran {_esc(hooks_last["finished"][11:])}</div>' + hi)
        hooks_html = (f'<h2>On every quit ({len(hooks)})</h2>'
                      f'<div class="age">these re-run each time KiCad closes</div>'
                      + hrows + hl)

    html = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Staged changes</title>
<style>
:root{{--bg:#1b1c1f;--panel:#232427;--line:#34363b;--fg:#d4d7dd;--muted:#8a8f98;
--err:#ff7b7b;--ok:#5fd38d;--warn:#ffcc66;--info:#7fa8d8;}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--fg);padding:16px;
font:13px/1.55 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}}
.state{{display:inline-block;padding:8px 16px;border-radius:8px;font-weight:700;font-size:15px;margin-bottom:6px}}
.state.open{{background:#3a3115;color:var(--warn)}}
.state.closed{{background:#143d28;color:var(--ok)}}
.state.run{{background:#1e2a38;color:var(--info)}}
.clock{{color:var(--muted);margin-left:10px;font-size:12px}}
h2{{font-size:12px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin:22px 0 8px}}
.job{{background:var(--panel);border:1px solid var(--line);border-left:3px solid var(--warn);
border-radius:7px;padding:8px 12px;margin:6px 0;display:flex;flex-direction:column;gap:2px}}
.job .lbl{{font-weight:600}} .job .cmd{{color:var(--info);font-size:12px}}
.job .age{{color:var(--muted);font-size:11px}}
.empty{{color:var(--muted);padding:8px 0}}
.res{{background:var(--panel);border:1px solid var(--line);border-left:3px solid var(--ok);
border-radius:7px;padding:8px 12px;margin:6px 0}}
.res.fail{{border-left-color:var(--err)}}
.resh{{display:flex;gap:10px;align-items:center}}
.rbadge{{font-size:10px;font-weight:700;padding:1px 7px;border-radius:4px;background:#143d28;color:var(--ok)}}
.res.fail .rbadge{{background:#3d1717;color:var(--err)}}
.res .lbl{{font-weight:600}}
pre{{margin:8px 0 0;padding:8px;background:#16171a;border-radius:6px;overflow:auto;
color:var(--muted);font-size:11px;white-space:pre-wrap}}
.locks{{color:var(--muted);font-size:11px;margin-top:8px}}
</style></head><body>
<div><span class="state {state_cls}">{_esc(state_txt)}</span><span class="clock">updated {now}</span></div>
<div class="locks">locks: {_esc(", ".join(locks)) if locks else "none"}</div>
<h2>Staged ({len(jobs)})</h2>
{''.join(rows)}
{last_html}
{hooks_html}
<script>setTimeout(()=>location.reload(),2500)</script>
</body></html>"""
    sp.status_html.write_text(html)


# ── daemon ──────────────────────────────────────────────────────────────────
def _read_pid(sp: StagePaths) -> int | None:
    if sp.pidfile.exists():
        try:
            return int(sp.pidfile.read_text().strip())
        except ValueError:
            return None
    return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def daemon(sp: StagePaths, grace: float, interval: float) -> int:
    sp.ensure_dirs()
    existing = _read_pid(sp)
    if existing and existing != os.getpid() and _pid_alive(existing):
        print(f"daemon already running (pid {existing})")
        return 1
    sp.pidfile.write_text(str(os.getpid()))
    print(f"stage-runner daemon up (pid {os.getpid()}); grace={grace}s poll={interval}s")
    closed_since: float | None = None
    hooks_done = False          # hooks run once per closed episode; reset on reopen
    label = f"{sp.project_name} Stage"
    try:
        while True:
            locks = locks_present(sp)
            open_now = bool(locks) and (kicad_running() if shutil.which("pgrep") else True)
            if open_now:
                closed_since = None
                hooks_done = False
            else:
                if closed_since is None:
                    closed_since = time.time()
                elif (time.time() - closed_since) >= grace:
                    # one-shot queue jobs — drain whenever something is queued
                    if queued_jobs(sp):
                        n = len(queued_jobs(sp))
                        write_status(sp, False, locks, pending_run=True)
                        results = drain_queue(sp)
                        ok = sum(1 for r in results if r["ok"])
                        fail = len(results) - ok
                        if fail:
                            notify(f"{label} ⚠️",
                                   f"{ok} ok, {fail} FAILED of {n} — check sidecar", "Basso")
                        else:
                            notify(f"{label} ✅",
                                   f"{ok} job(s) applied — safe to open KiCad", "Glass")
                    # recurring hooks — once per closed episode, run quietly
                    if not hooks_done and hooks_list(sp):
                        write_status(sp, False, locks, pending_run=True)
                        run_hooks(sp)
                        hooks_done = True
            write_status(sp, open_now, locks)
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        if _read_pid(sp) == os.getpid():
            sp.pidfile.unlink(missing_ok=True)
    return 0


# ── CLI (registered as `kb stage <subcommand>`) ──────────────────────────────
def add_parser(sub) -> None:
    # `--config` is shared by the parent `stage` parser AND every subcommand, so it works
    # in either position: `kb stage --config X status` and `kb stage status --config X`.
    cfg_parent = argparse.ArgumentParser(add_help=False)
    cfg_parent.add_argument("--config", help="path to kicad-bench.toml")

    p = sub.add_parser(
        "stage", parents=[cfg_parent],
        help="lock-aware job queue: apply edits when KiCad closes",
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.set_defaults(func=run)
    ssub = p.add_subparsers(dest="stage_cmd", metavar="<subcommand>")

    a = ssub.add_parser("add", parents=[cfg_parent], help="stage a job (label + command after --)")
    a.add_argument("--label", required=True)
    a.add_argument("rest", nargs=argparse.REMAINDER,
                   help="-- COMMAND ARGS… (run when KiCad closes)")

    ssub.add_parser("status", parents=[cfg_parent], help="one-line KiCad state + queue depth")
    ssub.add_parser("list", parents=[cfg_parent], help="show queue + last run")
    ssub.add_parser("clear", parents=[cfg_parent], help="drop all queued jobs")
    ssub.add_parser("run-now", parents=[cfg_parent],
                    help="force-drain the queue immediately (ignores lock state)")

    ha = ssub.add_parser("hook-add", parents=[cfg_parent],
                         help="register a RECURRING command (runs every KiCad-close)")
    ha.add_argument("--label", required=True)
    ha.add_argument("rest", nargs=argparse.REMAINDER, help="-- COMMAND ARGS…")
    ssub.add_parser("hook-list", parents=[cfg_parent],
                    help="show recurring on-quit hooks + their last run")
    hr = ssub.add_parser("hook-rm", parents=[cfg_parent], help="remove a hook by id, or 'all'")
    hr.add_argument("which", help="hook id (see hook-list) or 'all'")
    ssub.add_parser("hook-run", parents=[cfg_parent], help="run all hooks now (manual override)")

    d = ssub.add_parser("daemon", parents=[cfg_parent], help="run the watch loop")
    d.add_argument("--grace", type=float, default=GRACE_SEC)
    d.add_argument("--interval", type=float, default=POLL_SEC)


def _strip_dashdash(rest: list[str]) -> list[str]:
    return rest[1:] if rest and rest[0] == "--" else rest


def run(args) -> int:
    sp = paths_from_config(cfgmod.load_or_exit(getattr(args, "config", None)))
    cmd = getattr(args, "stage_cmd", None) or "status"

    if cmd == "add":
        job_cmd = _strip_dashdash(args.rest)
        if not job_cmd:
            print("error: no command — usage: kb stage add --label L -- python3 scripts/foo.py",
                  file=sys.stderr)
            return 2
        path = add_job(sp, args.label, job_cmd)
        write_status(sp, project_open(sp), locks_present(sp))
        print(f"staged: {args.label}  ->  {path.name}")
        return 0

    if cmd == "status":
        locks = locks_present(sp)
        open_now = project_open(sp)
        jobs = queued_jobs(sp)
        print(f"KiCad: {'OPEN' if open_now else 'CLOSED'}  ({len(locks)} lock(s))   "
              f"queued: {len(jobs)}   [{sp.project_name}]")
        for j in jobs:
            print(f"  • {j['label']}   [{' '.join(j['cmd'])}]")
        return 0

    if cmd == "list":
        jobs = queued_jobs(sp)
        print(f"queued ({len(jobs)}):")
        for j in jobs:
            print(f"  • [{j['added']}] {j['label']}  [{' '.join(j['cmd'])}]")
        last = _load_last_run(sp)
        if last:
            print(f"\nlast run {last['finished']}:")
            for r in last["results"]:
                print(f"  {'OK ' if r['ok'] else 'FAIL'} {r['label']}")
        return 0

    if cmd == "clear":
        n = clear_queue(sp)
        write_status(sp, project_open(sp), locks_present(sp))
        print(f"cleared {n} job(s)")
        return 0

    if cmd == "run-now":
        results = drain_queue(sp)
        ok = sum(1 for r in results if r["ok"])
        fail = len(results) - ok
        print(f"ran {len(results)} job(s): {ok} ok, {fail} failed")
        for r in results:
            print(f"  {'OK ' if r['ok'] else 'FAIL'} {r['label']}")
        write_status(sp, project_open(sp), locks_present(sp))
        if results:
            notify(f"{sp.project_name} Stage " + ("⚠️" if fail else "✅"),
                   f"{ok} ok, {fail} failed (run-now)", "Basso" if fail else "Glass")
        return 1 if fail else 0

    if cmd == "hook-add":
        hook_cmd = _strip_dashdash(args.rest)
        if not hook_cmd:
            print("error: no command — usage: kb stage hook-add --label L -- python3 scripts/foo.py",
                  file=sys.stderr)
            return 2
        path = hook_add(sp, args.label, hook_cmd)
        write_status(sp, project_open(sp), locks_present(sp))
        print(f"hook registered (runs on every KiCad-close): {args.label}  ->  {path.name}")
        return 0

    if cmd == "hook-list":
        hooks = hooks_list(sp)
        print(f"on-quit hooks ({len(hooks)}):")
        for h in hooks:
            print(f"  ↻ [{h['id']}] {h['label']}  [{' '.join(h['cmd'])}]")
        hl = _load_hooks_last(sp)
        if hl:
            print(f"\nlast ran {hl['finished']}:")
            for r in hl["results"]:
                print(f"  {'OK ' if r['ok'] else 'FAIL'} {r['label']}")
        return 0

    if cmd == "hook-rm":
        n = hook_clear(sp, args.which)
        write_status(sp, project_open(sp), locks_present(sp))
        print(f"removed {n} hook(s)")
        return 0

    if cmd == "hook-run":
        results = run_hooks(sp)
        ok = sum(1 for r in results if r["ok"])
        print(f"ran {len(results)} hook(s): {ok} ok, {len(results)-ok} failed")
        for r in results:
            print(f"  {'OK ' if r['ok'] else 'FAIL'} {r['label']}")
        return 0

    if cmd == "daemon":
        return daemon(sp, args.grace, args.interval)

    return 2
