# -*- coding: utf-8 -*-
"""
Auto-Continue Plugin for Hermes Agent.

Solves: tool iteration limit kills the agent mid-Phase, user must type "go on".

Mechanism:
1. Agent writes .hermes/phase-plan.md when starting a planned Phase
2. on_session_end: if interrupted + phase plan in_progress → write pending marker
3. pre_llm_call: if pending marker exists → inject "continue your unfinished task" context
4. Agent updates phase-plan.md as steps complete; sets Status: completed when done

Phase plan format (.hermes/phase-plan.md):
    # Phase Plan: <name>
    - Status: in_progress | completed
    - Started: <ISO timestamp>
    - Todo:
      - [x] Step 1: ...
      - [>] Step 2: ... (CURRENT)
      - [ ] Step 3: ...
    - NoUserDecisionsPending: true
"""

import json
import logging
import re
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# Paths
HERMES_HOME = Path.home() / "AppData" / "Local" / "hermes"
PENDING_MARKER = HERMES_HOME / "auto-continue-pending.json"
KNOWN_WORKDIRS_FILE = HERMES_HOME / "auto-continue-workdirs.txt"

# Default workdirs to scan for phase plans
_DEFAULT_WORKDIRS = [
    Path("D:/agent"),
    Path("D:/agent/project_anti_zero"),
]


def _find_phase_plan(workdir: Path) -> Path | None:
    """Find .hermes/phase-plan.md in a workdir."""
    candidate = workdir / ".hermes" / "phase-plan.md"
    return candidate if candidate.exists() else None


def _parse_phase_plan(path: Path) -> dict | None:
    """Parse a phase-plan.md into a dict."""
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return None

    status_m = re.search(r"^- Status:\s*(.+)$", text, re.MULTILINE)
    started_m = re.search(r"^- Started:\s*(.+)$", text, re.MULTILINE)
    no_decisions_m = re.search(r"^- NoUserDecisionsPending:\s*(true|false)", text, re.MULTILINE)
    title_m = re.search(r"^# Phase Plan:\s*(.+)$", text, re.MULTILINE)

    # Count todo items
    completed = len(re.findall(r"^- \[x\]", text, re.MULTILINE))
    current = len(re.findall(r"^- \[>\]", text, re.MULTILINE))
    pending = len(re.findall(r"^- \[ \]", text, re.MULTILINE))

    return {
        "title": title_m.group(1).strip() if title_m else "Unknown",
        "status": status_m.group(1).strip() if status_m else "unknown",
        "started": started_m.group(1).strip() if started_m else "",
        "no_user_decisions": no_decisions_m.group(1).strip().lower() == "true" if no_decisions_m else False,
        "steps_completed": completed,
        "steps_current": current,
        "steps_pending": pending,
        "path": str(path),
    }


def _scan_for_interrupted_phases() -> list[dict]:
    """Scan known workdirs for in-progress phase plans."""
    plans = []

    # Read configured workdirs
    workdirs = list(_DEFAULT_WORKDIRS)
    if KNOWN_WORKDIRS_FILE.exists():
        try:
            for line in KNOWN_WORKDIRS_FILE.read_text(encoding="utf-8").strip().splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    workdirs.append(Path(line))
        except Exception:
            pass

    for wd in workdirs:
        plan_path = _find_phase_plan(wd)
        if plan_path:
            parsed = _parse_phase_plan(plan_path)
            if parsed and parsed["status"] == "in_progress" and parsed["no_user_decisions"]:
                plans.append(parsed)

    return plans


def _clear_pending_marker():
    """Remove the pending marker file."""
    try:
        PENDING_MARKER.unlink(missing_ok=True)
    except Exception:
        pass


def _write_pending_marker(plans: list[dict]):
    """Write pending marker with phase info."""
    try:
        HERMES_HOME.mkdir(parents=True, exist_ok=True)
        PENDING_MARKER.write_text(
            json.dumps({
                "timestamp": datetime.now().isoformat(),
                "phases": plans,
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


def _read_pending_marker() -> dict | None:
    """Read pending marker if it exists and is fresh (< 30 min)."""
    if not PENDING_MARKER.exists():
        return None
    try:
        data = json.loads(PENDING_MARKER.read_text(encoding="utf-8"))
        ts = datetime.fromisoformat(data["timestamp"])
        age_minutes = (datetime.now() - ts).total_seconds() / 60
        if age_minutes > 30:
            _clear_pending_marker()
            return None
        return data
    except Exception:
        _clear_pending_marker()
        return None


# ──────────────────────────────────────────────────────────────────
# Plugin hooks
# ──────────────────────────────────────────────────────────────────

def on_session_end(session_id: str, completed: bool, interrupted: bool, **kwargs):
    """
    Detect interrupted sessions with active phase plans.
    Write a pending marker so pre_llm_call can inject context next turn.
    """
    if completed or not interrupted:
        # Session finished normally — clean up any stale marker
        _clear_pending_marker()
        return

    # Session was interrupted — check for in-progress phases
    plans = _scan_for_interrupted_phases()
    if plans:
        logger.info(
            "Auto-continue: session %s interrupted with %d active phase(s)",
            session_id, len(plans),
        )
        _write_pending_marker(plans)


def pre_llm_call(session_id: str, user_message: str, is_first_turn: bool, **kwargs):
    """
    Inject 'continue your unfinished task' context if a pending marker exists.
    Only inject on turns where the user hasn't explicitly said 'go on' already.
    """
    marker = _read_pending_marker()
    if not marker:
        return None

    plans = marker.get("phases", [])
    if not plans:
        return None

    # Don't double-inject if user is already saying go on
    user_lower = (user_message or "").lower().strip()
    if any(kw in user_lower for kw in ["go on", "continue", "继续", "接着"]):
        _clear_pending_marker()
        return None

    # Build context injection
    lines = ["⚠️ AUTO-CONTINUE: Your last session was interrupted mid-task. Resume your unfinished work:"]
    for plan in plans:
        total = plan["steps_completed"] + plan["steps_current"] + plan["steps_pending"]
        lines.append(
            f"  Phase '{plan['title']}': {plan['steps_completed']}/{total} steps done, "
            f"{plan['steps_pending']} remaining. Plan file: {plan['path']}"
        )
    lines.append("Continue from where you left off. Do NOT ask the user for confirmation — all decisions were already made.")

    # Clear marker so we don't re-inject
    _clear_pending_marker()

    return {"context": "\n".join(lines)}


def register(ctx):
    """Plugin registration."""
    ctx.register_hook("on_session_end", on_session_end)
    ctx.register_hook("pre_llm_call", pre_llm_call)
