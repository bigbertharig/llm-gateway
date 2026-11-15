"""Plan discovery and input defaults owned by gateway."""

import re
from datetime import datetime
from pathlib import Path
from typing import Any

from .sources import _json

load_json = _json


def shoulders_dir(shared_path: Path) -> Path:
    """Get the shoulders plan directory."""
    return shared_path / "plans" / "shoulders"


def arms_dir(shared_path: Path) -> Path:
    """Get the arms plan directory."""
    return shared_path / "plans" / "arms"


def plans_dir_for_scope(shared_path: Path, plan_scope: str) -> Path:
    """Get plan directory for a supported scope."""
    scope = str(plan_scope or "").strip().lower()
    if scope == "arms":
        return arms_dir(shared_path)
    return shoulders_dir(shared_path)


def plan_dir(shared_path: Path, plan_name: str, plan_scope: str = "shoulders") -> Path:
    """Get directory for a specific plan in a given scope."""
    return plans_dir_for_scope(shared_path, plan_scope) / plan_name


def discover_plans(shared_path: Path, plan_scope: str = "shoulders") -> list[str]:
    """Discover available plans in selected scope directory."""
    plans_dir = plans_dir_for_scope(shared_path, plan_scope)
    out: list[str] = []
    if not plans_dir.exists():
        return out
    for p in sorted(plans_dir.iterdir()):
        if not p.is_dir():
            continue
        has_scripts = (p / "scripts").exists()
        has_root_plan = (p / "plan.md").exists()
        has_input_starter = False
        input_dir = p / "input"
        if input_dir.exists():
            has_input_starter = any(
                md.is_file() and (md.name == "plan.md" or md.name.endswith("_plan.md"))
                for md in input_dir.glob("*.md")
            )
        if has_scripts and (has_root_plan or has_input_starter):
            out.append(p.name)
    return out


def discover_plan_starters(shared_path: Path, plan_name: str, plan_scope: str = "shoulders") -> list[str]:
    """Discover starter plan files for a plan."""
    plan_path = plan_dir(shared_path, plan_name, plan_scope)
    starters: set[str] = set()
    if not plan_path.exists():
        return []
    for md in plan_path.glob("*.md"):
        if md.is_file() and (md.name == "plan.md" or md.name.endswith("_plan.md")):
            starters.add(md.name)
    input_dir = plan_path / "input"
    if input_dir.exists():
        for md in input_dir.rglob("*.md"):
            if md.is_file() and (md.name == "plan.md" or md.name.endswith("_plan.md")):
                starters.add(str(Path("input") / md.relative_to(input_dir)))
    return sorted(starters)


def discover_plan_input_files(
    shared_path: Path,
    plan_name: str,
    limit: int = 200,
    plan_scope: str = "shoulders",
) -> list[str]:
    """Discover input files for a plan."""
    plan_path = plan_dir(shared_path, plan_name, plan_scope)
    input_dir = plan_path / "input"
    if not input_dir.exists():
        return []
    out: list[str] = []
    for p in sorted(input_dir.rglob("*")):
        if len(out) >= limit:
            break
        if not p.is_file():
            continue
        if ".submit_runtime" in p.parts:
            continue
        rel = p.relative_to(shared_path)
        out.append(f"/mnt/shared/{rel.as_posix()}")
    return out


def discover_active_arm_bindings(shared_path: Path) -> dict[str, dict[str, Any]]:
    """Discover shoulder->arm bindings from active_arms.json files."""
    arms_available = set(discover_plans(shared_path, plan_scope="arms"))
    out: dict[str, dict[str, Any]] = {}
    for shoulder in discover_plans(shared_path, plan_scope="shoulders"):
        bindings_path = plan_dir(shared_path, shoulder, plan_scope="shoulders") / "bindings" / "active_arms.json"
        payload = load_json(bindings_path)
        if not isinstance(payload, dict):
            continue
        rows: list[dict[str, str]] = []
        for arm_row in payload.get("arms", []) or []:
            if not isinstance(arm_row, dict):
                continue
            arm_name = str(arm_row.get("name", "")).strip()
            if not arm_name or arm_name not in arms_available:
                continue
            starters = discover_plan_starters(shared_path, arm_name, plan_scope="arms")
            configured = str(arm_row.get("starter", "")).strip()
            if configured and configured in starters:
                starter = configured
            else:
                starter = "plan.md" if "plan.md" in starters else (starters[0] if starters else "")
            rows.append({"name": arm_name, "starter": starter})
        if not rows:
            continue
        default_arm = str(payload.get("default_arm", "")).strip()
        if default_arm and default_arm not in {r["name"] for r in rows}:
            default_arm = ""
        out[shoulder] = {"default_arm": default_arm, "arms": rows}
    return out


def write_inline_input_file(shared_path: Path, plan_name: str, text: str, plan_scope: str = "shoulders") -> str:
    """Write inline input content to a runtime file."""
    runtime_dir = (
        plan_dir(shared_path, plan_name, plan_scope)
        / "input"
        / ".submit_runtime"
        / "dashboard"
    )
    runtime_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    path = runtime_dir / f"inline_{stamp}.md"
    payload = text + ("\n" if text and not text.endswith("\n") else "")
    path.write_text(payload, encoding="utf-8")
    rel = path.relative_to(shared_path)
    return f"/mnt/shared/{rel.as_posix()}"


def default_plan_config(shared_path: Path, plan_name: str) -> dict[str, Any]:
    """Get default configuration for a plan."""
    if plan_name == "research_assistant":
        return {
            "QUERY_FILE": "/mnt/shared/plans/shoulders/research_assistant/input/query.md",
            "TARGET_COUNT": "20",
            "TARGET_TOLERANCE": "0",
            "SECONDARY_TARGET_COUNT": "20",
            "SEARCH_DEPTH": "basic",
            "OUTPUT_FORMAT": "both",
            "RUN_MODE": "fresh",
            "PRIORITY": "normal",
            "PREEMPTIBLE": True,
        }
    if plan_name == "dc_integration":
        return {
            "ZIM_PATH": "/mnt/shared/path/to/archive.zim",
            "SOURCE_ID": "source_name",
            "OUTPUT_FOLDER": "/mnt/shared/plans/shoulders/dc_integration/output",
            "RUN_MODE": "fresh",
            "PRIORITY": "normal",
            "PREEMPTIBLE": True,
        }
    if plan_name == "github_analyzer":
        return {
            "REPO_PATH": "",
            "REPO_URL": "",
            "CLAIMED_BEHAVIOR": "",
            "ANALYSIS_DEPTH": "standard",
            "HOT_WORKERS": "auto",
            "WORKER_MODEL": "qwen2.5:7b",
            # Advanced review pipeline defaults (kept explicit so plan placeholders resolve
            # even when dashboard submit UI only exposes the simple inputs).
            "DOC_WORKER_MODEL": "qwen2.5:7b",
            "REVIEW_WORKER_MODEL": "qwen2.5-coder:14b",
            "REVIEW_EXTRACTOR_MODEL": "qwen2.5:7b",
            "REVIEW_VERIFIER_MODEL": "qwen2.5-coder:14b",
            "REVIEW_ADJUDICATOR_MODEL": "qwen2.5-coder:14b",
            "REVIEW_EXTRACTOR_FALLBACK_MODEL": "qwen2.5-coder:14b",
            "REVIEW_VERIFIER_FALLBACK_MODEL": "qwen2.5:7b",
            "REVIEW_ADJUDICATOR_FALLBACK_MODEL": "qwen2.5:7b",
            "WORKER_SHARDS": "5",
            "WORKER_CONTEXT_TOKENS": "8192",
            "WORKER_CONTEXT_UTILIZATION": "0.75",
            "MAX_REPORT_ATTEMPTS": "3",
            "RUN_MODE": "fresh",
            "PRIORITY": "normal",
            "PREEMPTIBLE": True,
        }
    if plan_name == "research_prospector":
        return {
            "QUERY_FILE": "/mnt/shared/plans/arms/research_prospector/input/runs/query_candidate_d_20260220_with_sources.md",
            "TARGET_COUNT": "20",
            "TARGET_TOLERANCE": "0",
            "SECONDARY_TARGET_COUNT": "20",
            "SEARCH_DEPTH": "basic",
            "DISCOVERY_ROUNDS": "0",
            "MAX_QUERIES_PER_ROUND": "10",
            "LOW_YIELD_USEFUL_THRESHOLD": "2",
            "LOW_YIELD_CONSECUTIVE_ROUNDS": "2",
            "PRE_SCRAPE_MIN_CONFIDENCE": "60",
            "PRE_SCRAPE_PER_SOURCE_CAP": "3",
            "MAX_SCRAPE_CANDIDATES": "0",
            "SCRAPE_PERSON_TIMEOUT": "180",
            "SCORE_TRIAGE_TIMEOUT": "180",
            "SCORE_TRIAGE_ATTEMPTS": "2",
            "OUTPUT_FORMAT": "both",
            "RUN_MODE": "fresh",
            "PRIORITY": "normal",
            "PREEMPTIBLE": True,
        }
    if plan_name == "research_contact_enrichment":
        return {
            "INPUT_FILE": "/mnt/shared/plans/arms/research_contact_enrichment/input/sample_contacts.csv",
            "SEARCH_DEPTH": "basic",
            "OUTPUT_FORMAT": "both",
            "RUN_MODE": "fresh",
            "PRIORITY": "normal",
            "PREEMPTIBLE": True,
        }
    if plan_name == "research_topic_research":
        return {
            "TOPIC": "example topic",
            "DAYS": "30",
            "DEPTH": "default",
            "SEARCH_MODE": "daily",
            "STOP_TERMS": "",
            "BLOCKED_DOMAINS": "",
            "RUN_MODE": "fresh",
            "PRIORITY": "normal",
            "PREEMPTIBLE": True,
        }
    if plan_name == "research_event_discovery":
        return {
            "QUERY": "floods in the US in the past 5 years",
            "MAX_EVENTS": "20",
            "MIN_SIGNIFICANCE": "50",
            "OUTPUT_FORMAT": "both",
            "RUN_MODE": "fresh",
            "PRIORITY": "normal",
            "PREEMPTIBLE": True,
        }
    return {"RUN_MODE": "fresh", "PRIORITY": "normal", "PREEMPTIBLE": True}


def plan_input_help(
    shared_path: Path,
    plan_name: str,
    starter_file: str | None = None,
    plan_scope: str = "shoulders",
) -> list[dict[str, str]]:
    """Extract input help from plan.md ## Inputs section."""
    plan_path = plan_dir(shared_path, plan_name, plan_scope)
    if starter_file:
        plan_md = plan_path / starter_file
    else:
        plan_md = plan_path / "plan.md"
    if not plan_md.exists() and not starter_file:
        starters = discover_plan_starters(shared_path, plan_name, plan_scope=plan_scope)
        if starters:
            plan_md = plan_path / starters[0]
    if not plan_md.exists():
        return []
    try:
        text = plan_md.read_text(encoding="utf-8")
    except Exception:
        return []

    lines = text.splitlines()
    in_inputs = False
    out: list[dict[str, str]] = []
    for line in lines:
        if line.strip().lower() == "## inputs":
            in_inputs = True
            continue
        if in_inputs and line.startswith("## "):
            break
        if not in_inputs:
            continue
        m = re.match(r"^\s*-\s+\*\*(.+?)\*\*:\s*(.+)\s*$", line)
        if not m:
            continue
        key = m.group(1).strip()
        desc = m.group(2).strip()
        options = ", ".join(re.findall(r"`([^`]+)`", desc))
        out.append({
            "key": key,
            "description": desc,
            "options": options,
        })
    return out
