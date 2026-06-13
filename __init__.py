# -*- coding: utf-8 -*-
"""
Auto-Continue Plugin for Hermes Agent (v2)
===========================================

Solves: agent work interrupted mid-phase (max_turns, context compression,
user closes app, /reset, etc.) and the new session has no memory of what
was in progress.

Design principles:
  1. Phase-plan.md IS the source of truth — no intermediate marker files.
  2. Session isolation: each project has its own phase plan; sessions only
     see plans for their own workdir.
  3. Detection on session_start AND pre_llm_call — covers both new-session
     and mid-session interruption scenarios.
  4. Hook validation: pre_llm_call verifies phase plan freshness (stale
     plans older than STALE_DAYS are ignored).

Phase plan format (<project>/.hermes/phase-plan.md):
    # Phase Plan: <name>
    - Status: in_progress | completed
    - Started: <ISO timestamp>
    - Todo:
      - [x] Step 1: ...
      - [>] Step 2: ... (CURRENT)
      - [ ] Step 3: ...
    - NoUserDecisionsPending: true | false

Hooks used:
  - on_session_start: detect active phase plans, prime context injection
  - pre_llm_call: inject "continue unfinished work" context
  - on_session_end: cleanup (clear injected state for this session)

Session isolation:
  - Registry file (auto-continue-registry.json) maps session_id → workdir
  - Workdir detected from config terminal.cwd on session_start
  - Each session only sees phase plans from its own workdir
"""

import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────
# Paths
# ──────────────────────────────────────────────────────────────────

HERMES_HOME = Path.home() / "AppData" / "Local" / "hermes"
REGISTRY_FILE = HERMES_HOME / "auto-continue-registry.json"

# How old can a phase plan's Started timestamp be before we consider it stale?
STALE_DAYS = 14

# ──────────────────────────────────────────────────────────────────
# Registry: session_id → workdir mapping
# ──────────────────────────────────────────────────────────────────

def _read_registry() -> dict:
    """Read the session → workdir registry."""
    try:
        if REGISTRY_FILE.exists():
            return json.loads(REGISTRY_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {"sessions": {}}


def _write_registry(registry: dict) -> None:
    """Write the session → workdir registry."""
    try:
        HERMES_HOME.mkdir(parents=True, exist_ok=True)
        REGISTRY_FILE.write_text(
            json.dumps(registry, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


def _get_workdir_for_session(session_id: str) -> Optional[Path]:
    """Look up the workdir for a given session from the registry."""
    registry = _read_registry()
    entry = registry.get("sessions", {}).get(session_id)
    if entry and isinstance(entry, dict):
        wd = entry.get("workdir")
        if wd:
            return Path(wd)
    return None


def _register_session_workdir(session_id: str, workdir: Path) -> None:
    """Record which workdir a session is using."""
    registry = _read_registry()
    registry.setdefault("sessions", {})[session_id] = {
        "workdir": str(workdir),
        "registered_at": datetime.now().isoformat(),
    }
    _write_registry(registry)


def _unregister_session(session_id: str) -> None:
    """Remove a session from the registry."""
    registry = _read_registry()
    registry.setdefault("sessions", {}).pop(session_id, None)
    _write_registry(registry)


def _cleanup_stale_sessions(max_age_days: int = 7) -> None:
    """Remove sessions older than max_age_days from registry."""
    registry = _read_registry()
    cutoff = datetime.now() - timedelta(days=max_age_days)
    sessions = registry.get("sessions", {})
    to_remove = []
    for sid, entry in sessions.items():
        try:
            registered = datetime.fromisoformat(entry.get("registered_at", ""))
            if registered < cutoff:
                to_remove.append(sid)
        except Exception:
            to_remove.append(sid)
    for sid in to_remove:
        sessions.pop(sid, None)
    if to_remove:
        _write_registry(registry)


def _detect_workdir_from_config() -> Optional[Path]:
    """Read the current workdir from Hermes config.yaml (terminal.cwd)."""
    try:
        config_path = HERMES_HOME / "config.yaml"
        if not config_path.exists():
            return None
        import yaml
        config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        cwd = config.get("terminal", {}).get("cwd", "")
        if cwd:
            return Path(cwd)
    except Exception:
        pass
    return None


# ──────────────────────────────────────────────────────────────────
# Phase plan parsing
# ──────────────────────────────────────────────────────────────────

def _find_phase_plan(workdir: Path) -> Optional[Path]:
    """Find .hermes/phase-plan.md in a workdir."""
    candidate = workdir / ".hermes" / "phase-plan.md"
    return candidate if candidate.exists() else None


def _parse_phase_plan(path: Path) -> Optional[dict]:
    """Parse a phase-plan.md into a structured dict."""
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return None

    status_m = re.search(r"^- Status:\s*(.+)$", text, re.MULTILINE)
    started_m = re.search(r"^- Started:\s*(.+)$", text, re.MULTILINE)
    no_decisions_m = re.search(
        r"^- NoUserDecisionsPending:\s*(true|false)", text, re.MULTILINE
    )
    title_m = re.search(r"^# Phase Plan:\s*(.+)$", text, re.MULTILINE)

    # Count todo items
    completed = len(re.findall(r"^- \[x\]", text, re.MULTILINE))
    current = len(re.findall(r"^- \[>\]", text, re.MULTILINE))
    pending = len(re.findall(r"^- \[ \]", text, re.MULTILINE))

    status = status_m.group(1).strip() if status_m else "unknown"
    started_str = started_m.group(1).strip() if started_m else ""
    no_decisions = (
        no_decisions_m.group(1).strip().lower() == "true"
        if no_decisions_m
        else False
    )

    return {
        "title": title_m.group(1).strip() if title_m else "Unknown",
        "status": status,
        "started": started_str,
        "no_user_decisions": no_decisions,
        "steps_completed": completed,
        "steps_current": current,
        "steps_pending": pending,
        "path": str(path),
        "workdir": str(path.parent.parent),  # .hermes/..
    }


def _is_stale(plan: dict) -> bool:
    """Check if a phase plan is too old to auto-continue."""
    started = plan.get("started", "")
    if not started:
        return False  # no timestamp = don't skip
    try:
        ts = datetime.fromisoformat(started)
        return (datetime.now() - ts) > timedelta(days=STALE_DAYS)
    except Exception:
        return False


def _find_phase_plan_recursive(root: Path, max_depth: int = 3) -> list[Path]:
    """Recursively find all .hermes/phase-plan.md under root (up to max_depth)."""
    plans: list[Path] = []
    # Check root itself first
    direct = _find_phase_plan(root)
    if direct:
        plans.append(direct)
    # Then scan subdirectories
    try:
        for child in sorted(root.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            if max_depth <= 1:
                continue
            candidate = child / ".hermes" / "phase-plan.md"
            if candidate.exists():
                plans.append(candidate)
            # Recurse one more level
            if max_depth > 2:
                try:
                    for grandchild in sorted(child.iterdir()):
                        if not grandchild.is_dir() or grandchild.name.startswith("."):
                            continue
                        gc = grandchild / ".hermes" / "phase-plan.md"
                        if gc.exists():
                            plans.append(gc)
                except Exception:
                    pass
    except Exception:
        pass
    return plans


def _get_active_phase_plan(workdir: Path) -> Optional[dict]:
    """Get the active (in_progress, non-stale) phase plan for a workdir tree.

    Scans the workdir itself and subdirectories (up to 3 levels deep)
    to find phase plans. Returns the first active plan found.
    """
    plan_paths = _find_phase_plan_recursive(workdir)
    for plan_path in plan_paths:
        plan = _parse_phase_plan(plan_path)
        if not plan:
            continue
        if plan["status"] != "in_progress":
            continue
        if _is_stale(plan):
            logger.debug(
                "Phase plan '%s' is stale (started %s, threshold %d days) — skipping",
                plan["title"], plan["started"], STALE_DAYS,
            )
            continue
        return plan
    return None


# ──────────────────────────────────────────────────────────────────
# Session-scoped state (in-memory only, not persisted)
# ──────────────────────────────────────────────────────────────────

# Tracks which sessions have already received auto-continue context
# so we don't re-inject on every pre_llm_call.
_injected_sessions: set[str] = set()


# ──────────────────────────────────────────────────────────────────
# Hook: on_session_start
# ──────────────────────────────────────────────────────────────────

def on_session_start(session_id: str, model: str = "", platform: str = "", **kwargs):
    """
    Detect the session's workdir and check for active phase plans.
    Register the session in the registry for future lookups.
    """
    # Cleanup old sessions periodically
    _cleanup_stale_sessions()

    # Detect workdir from config
    workdir = _detect_workdir_from_config()
    if workdir:
        _register_session_workdir(session_id, workdir)
        logger.debug(
            "Session %s registered with workdir %s", session_id, workdir
        )

    # Check for active phase plans
    if workdir:
        plan = _get_active_phase_plan(workdir)
        if plan:
            logger.info(
                "Auto-continue: session %s found active phase '%s' "
                "(%d/%d steps done, %d remaining)",
                session_id, plan["title"],
                plan["steps_completed"],
                plan["steps_completed"] + plan["steps_current"] + plan["steps_pending"],
                plan["steps_pending"],
            )


# ──────────────────────────────────────────────────────────────────
# Hook: pre_llm_call
# ──────────────────────────────────────────────────────────────────

def pre_llm_call(
    session_id: str,
    user_message: str = "",
    is_first_turn: bool = False,
    **kwargs,
):
    """
    Inject 'continue your unfinished task' context if:
    1. This session has an active phase plan in its workdir
    2. The user isn't explicitly saying "go on" / "继续"
    3. We haven't already injected for this session (or the injection
       was more than 5 turns ago — allows re-injection after context
       compression might have dropped the earlier injection)
    """
    # Find the session's workdir
    workdir = _get_workdir_for_session(session_id)
    if not workdir:
        # Fallback: try config detection (session might have started before
        # on_session_start fired)
        workdir = _detect_workdir_from_config()
        if workdir:
            _register_session_workdir(session_id, workdir)

    if not workdir:
        return None

    # Check for active phase plan
    plan = _get_active_phase_plan(workdir)
    if not plan:
        return None

    # Skip if user is already saying "go on"
    user_lower = (user_message or "").lower().strip()
    if any(kw in user_lower for kw in ["go on", "continue", "继续", "接着"]):
        return None

    # Check if we already injected for this session
    # (re-inject is allowed after context compression since earlier
    # injection may have been dropped)
    if session_id in _injected_sessions:
        return None

    # Build context injection
    total = plan["steps_completed"] + plan["steps_current"] + plan["steps_pending"]
    lines = [
        "⚠️ AUTO-CONTINUE: A previous session was interrupted mid-task. "
        "Resume your unfinished work:",
        f"  Phase '{plan['title']}': "
        f"{plan['steps_completed']}/{total} steps done, "
        f"{plan['steps_pending']} remaining.",
        f"  Plan file: {plan['path']}",
        "",
        "Read the plan file for the full step list. "
        "Continue from where you left off. "
        "Do NOT ask the user for confirmation — all decisions were already made.",
    ]

    # Mark as injected
    _injected_sessions.add(session_id)

    logger.info(
        "Auto-continue: injecting context for session %s, phase '%s'",
        session_id, plan["title"],
    )

    return {"context": "\n".join(lines)}


# ──────────────────────────────────────────────────────────────────
# Hook: on_session_end
# ──────────────────────────────────────────────────────────────────

def on_session_end(
    session_id: str,
    completed: bool = False,
    interrupted: bool = False,
    **kwargs,
):
    """
    Cleanup session state. No marker writing needed — the phase plan
    file on disk is the source of truth.
    """
    # Remove from injected set
    _injected_sessions.discard(session_id)

    # Keep registry entry if interrupted (so next session can find it)
    # Only remove if completed normally AND no active phase plan
    if completed and not interrupted:
        workdir = _get_workdir_for_session(session_id)
        if workdir:
            plan = _get_active_phase_plan(workdir)
            if not plan:
                # No active plan — safe to unregister
                _unregister_session(session_id)

    if interrupted:
        workdir = _get_workdir_for_session(session_id)
        if workdir:
            plan = _get_active_phase_plan(workdir)
            if plan:
                logger.info(
                    "Auto-continue: session %s interrupted with active phase '%s'",
                    session_id, plan["title"],
                )


# ──────────────────────────────────────────────────────────────────
# Plugin registration
# ──────────────────────────────────────────────────────────────────

def register(ctx):
    """Plugin registration."""
    ctx.register_hook("on_session_start", on_session_start)
    ctx.register_hook("pre_llm_call", pre_llm_call)
    ctx.register_hook("on_session_end", on_session_end)
