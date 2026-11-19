"""Gateway-owned plan selection, preparation, and submission requests."""

import json
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any

from .plan_catalog import (default_plan_config, discover_active_arm_bindings, discover_plan_input_files, discover_plan_starters, discover_plans, plan_input_help, write_inline_input_file)
from .plan_validation import (INLINE_FILE_KEYS, normalize_github_url, sanitize_config_object, sanitize_text)
from .sources import _json, orchestration_action, orchestration_summary

BATCH_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
GITHUB_URL_PARSE_RE = re.compile(r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)/?$")
STDIO_SNIPPET_LIMIT = 16000
STDIO_TIMEOUT_LIMIT = 8000
def load_brain_state(shared: Path) -> dict[str, Any]:
    return _json(shared / "brain" / "state.json") or {}


def _clip_stdio(text: str, limit: int = STDIO_SNIPPET_LIMIT) -> str:
    """Preserve both head and tail of long output (useful for tracebacks)."""
    if not isinstance(text, str):
        return ""
    if limit <= 0 or len(text) <= limit:
        return text
    head = max(1, int(limit * 0.45))
    tail = max(1, int(limit * 0.45))
    omitted = len(text) - head - tail
    return (
        text[:head]
        + f"\n\n...[truncated {omitted} chars]...\n\n"
        + text[-tail:]
    )


class PlanRequests:
    def __init__(self, shared_path: Path, config: dict[str, Any]):
        self.shared_path = Path(shared_path)
        self.config = config


    def options(self) -> dict[str, Any]:
        return self._control_options()


    def start_plan(self, plan_name: str, config_json: str = "", plan_scope: str = "shoulders",
                   starter_file: str = "", repo_path: str = "", repo_url: str = "", claimed_behavior: str = "",
                   inline_query_text: str = "", gateway_model_assignments: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._start_plan(plan_name, config_json, plan_scope, starter_file, repo_path, repo_url,
                                claimed_behavior, inline_query_text, gateway_model_assignments)


    def resume_plan(self, batch_id: str) -> dict[str, Any]:
        return self._resume_plan(batch_id)


    def _gpu_ssh(self, remote_cmd: str, timeout_s: int = 180) -> dict[str, Any]:
        # Pass remote command as a single shell-quoted argument so ssh does not
        # split `bash -lc` arguments and silently execute a different command.
        cmd = ["ssh", "-o", "BatchMode=yes", "gpu", f"bash -lc {shlex.quote(remote_cmd)}"]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
            return {
                "ok": proc.returncode == 0,
                "returncode": proc.returncode,
                "stdout": _clip_stdio(proc.stdout),
                "stderr": _clip_stdio(proc.stderr),
                "cmd": " ".join(cmd),
            }
        except subprocess.TimeoutExpired as e:
            return {
                "ok": False,
                "returncode": -1,
                "stdout": _clip_stdio(e.stdout or "", STDIO_TIMEOUT_LIMIT) if isinstance(e.stdout, str) else "",
                "stderr": _clip_stdio(e.stderr or "", STDIO_TIMEOUT_LIMIT) if isinstance(e.stderr, str) else "",
                "cmd": " ".join(cmd),
                "error": f"timeout after {timeout_s}s",
            }


    def _gpu_has_git(self) -> tuple[bool, str]:
        """Return whether git is available on the GPU host plus diagnostics."""
        out = self._gpu_ssh("command -v git >/dev/null 2>&1 && echo __OK__ || echo __MISSING__", timeout_s=20)
        stdout = str(out.get("stdout", ""))
        if out.get("ok") and "__OK__" in stdout:
            return True, ""
        detail = str(out.get("stderr", "")).strip() or stdout.strip() or "git not found on gpu host"
        return False, detail


    def _gpu_find_existing_repo_checkout(self, repo_url: str) -> tuple[str, str]:
        """Return (repo_path, normalized_url) for an existing local checkout on GPU host if found."""
        normalized = normalize_github_url(repo_url)
        m = GITHUB_URL_PARSE_RE.match(normalized)
        if not m:
            return "", normalized
        owner, repo = m.group(1), m.group(2)
        repo_base = f"{owner}-{repo}"
        candidates = [
            f"/mnt/shared/plans/arms/{repo_base}",
            f"/mnt/shared/plans/arms/exploratory/{repo_base}",
            f"/media/bryan/shared/plans/arms/{repo_base}",
            f"/media/bryan/shared/plans/arms/exploratory/{repo_base}",
        ]
        script_lines = ["set -e"]
        for path in candidates:
            qpath = shlex.quote(path)
            script_lines.append(f"if [ -d {qpath}/.git ]; then printf '%s\\n' {qpath}; exit 0; fi")
        script_lines.append("exit 1")
        out = self._gpu_ssh("\n".join(script_lines), timeout_s=20)
        if out.get("ok"):
            line = str(out.get("stdout", "") or "").strip().splitlines()
            if line and line[0].strip():
                return line[0].strip(), normalized
        return "", normalized


    def _discover_local_repo_choices(self) -> list[dict[str, str]]:
        """Discover git checkouts under arms/shoulders for dropdown selection."""
        plans_root = self.shared_path / "plans"
        seen: set[str] = set()
        out: list[dict[str, str]] = []

        def _to_mnt_path(p: Path) -> str:
            text = str(p)
            if text.startswith("/media/bryan/shared/"):
                return "/mnt/shared/" + text[len("/media/bryan/shared/"):]
            return text

        candidate_dirs: list[tuple[str, Path]] = [
            ("shoulders", plans_root / "shoulders"),
            ("arms", plans_root / "arms"),
            ("arms/exploratory", plans_root / "arms" / "exploratory"),
        ]

        for scope_label, base in candidate_dirs:
            try:
                if not base.exists() or not base.is_dir():
                    continue
            except Exception:
                continue
            try:
                children = sorted(base.iterdir())
            except Exception:
                continue
            for child in children:
                try:
                    if not child.is_dir():
                        continue
                    if not (child / ".git").is_dir():
                        continue
                except Exception:
                    continue
                mnt_path = _to_mnt_path(child)
                if mnt_path in seen:
                    continue
                seen.add(mnt_path)
                out.append(
                    {
                        "path": mnt_path,
                        "label": f"{scope_label}: {child.name} ({mnt_path})",
                        "scope": scope_label,
                        "name": child.name,
                    }
                )

        out.sort(key=lambda r: (str(r.get("scope", "")), str(r.get("name", ""))))
        return out


    def _control_options(self) -> dict[str, Any]:
        brain = load_brain_state(self.shared_path)
        active = brain.get("active_batches", {}) if isinstance(brain.get("active_batches"), dict) else {}
        shoulder_plans = discover_plans(self.shared_path, plan_scope="shoulders")
        arm_plans = discover_plans(self.shared_path, plan_scope="arms")
        plans = shoulder_plans + [p for p in arm_plans if p not in set(shoulder_plans)]
        plan_scopes: dict[str, str] = {}
        for p in shoulder_plans:
            plan_scopes.setdefault(p, "shoulders")
        for p in arm_plans:
            plan_scopes[p] = "arms"
        shoulder_arm_bindings = discover_active_arm_bindings(self.shared_path)
        recent_batches = orchestration_summary().get("recent_batches", [])
        plan_defaults = {p: default_plan_config(self.shared_path, p) for p in plans}
        plan_starters = {p: discover_plan_starters(self.shared_path, p, plan_scope=plan_scopes.get(p, "shoulders")) for p in plans}
        plan_input_files = {p: discover_plan_input_files(self.shared_path, p, plan_scope=plan_scopes.get(p, "shoulders")) for p in plans}
        plan_default_starter = {p: ("plan.md" if "plan.md" in plan_starters[p] else (plan_starters[p][0] if plan_starters[p] else "")) for p in plans}
        plan_inputs = {
            p: {starter: plan_input_help(self.shared_path, p, starter, plan_scope=plan_scopes.get(p, "shoulders")) for starter in plan_starters[p]}
            for p in plans
        }
        plan_defaults = {p: self._dashboard_visible_plan_defaults(p, cfg) for p, cfg in plan_defaults.items()}
        plan_inputs = {
            p: {starter: self._dashboard_visible_plan_inputs(p, rows) for starter, rows in starters.items()}
            for p, starters in plan_inputs.items()
        }
        active_meta = []
        batch_labels: dict[str, str] = {}
        for batch_id, meta in active.items():
            if not isinstance(meta, dict):
                continue
            plan_name = str(meta.get("plan", "")).strip() or "unknown_plan"
            display_name = str(meta.get("display_name", "")).strip() or plan_name
            active_meta.append(
                {
                    "batch_id": batch_id,
                    "plan": plan_name,
                    "display_name": display_name,
                    "scope": "arms" if "/plans/arms/" in str(meta.get("plan_dir", "")) else "shoulders",
                    "started_at": meta.get("started_at", ""),
                }
            )
            batch_labels[batch_id] = f"{display_name} | {batch_id}"
        for row in recent_batches:
            batch_id = str(row.get("batch_id", "")).strip()
            if not batch_id:
                continue
            display_name = str(row.get("display_name", "")).strip() or str(row.get("plan", "")).strip() or "unknown_plan"
            batch_labels.setdefault(batch_id, f"{display_name} | {batch_id}")
        active_meta.sort(key=lambda x: str(x.get("started_at") or ""), reverse=True)
        return {
            "ok": True,
            "active_batches": sorted(active.keys()),
            "active_batches_meta": active_meta,
            "resumable_batches": sorted(active.keys()),
            "recent_batches": recent_batches,
            "plans": plans,
            "shoulder_plans": shoulder_plans,
            "arm_plans": arm_plans,
            "plan_scopes": plan_scopes,
            "shoulder_arm_bindings": shoulder_arm_bindings,
            "plan_defaults": plan_defaults,
            "plan_starters": plan_starters,
            "plan_input_files": plan_input_files,
            "plan_default_starter": plan_default_starter,
            "plan_inputs": plan_inputs,
            "batch_labels": batch_labels,
            "local_repo_choices": self._discover_local_repo_choices(),
        }


    def _dashboard_visible_plan_defaults(self, plan_name: str, cfg: Any) -> dict[str, Any]:
        if not isinstance(cfg, dict):
            return {}
        if plan_name != "github_analyzer":
            return dict(cfg)
        keep = {"REPO_PATH", "REPO_URL", "CLAIMED_BEHAVIOR", "ANALYSIS_DEPTH"}
        return {k: v for k, v in cfg.items() if k in keep}


    def _dashboard_visible_plan_inputs(self, plan_name: str, rows: Any) -> list[dict[str, Any]]:
        if not isinstance(rows, list):
            return []
        if plan_name != "github_analyzer":
            return [r for r in rows if isinstance(r, dict)]
        keep = {"REPO_PATH", "REPO_URL", "CLAIMED_BEHAVIOR", "ANALYSIS_DEPTH"}
        out: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            key = str(row.get("key", "")).strip()
            if key in keep:
                out.append(dict(row))
        return out


    def _start_plan(
        self,
        plan_name: str,
        config_json: str,
        plan_scope: str = "shoulders",
        starter_file: str = "",
        repo_path: str = "",
        repo_url: str = "",
        claimed_behavior: str = "",
        inline_query_text: str = "",
        gateway_model_assignments: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not plan_name:
            return {"ok": False, "message": "plan_name is required"}
        scope = str(plan_scope or "").strip().lower()
        if scope not in {"shoulders", "arms"}:
            return {"ok": False, "message": f"invalid plan_scope: {plan_scope}"}

        plans = set(discover_plans(self.shared_path, plan_scope=scope))
        if plan_name not in plans:
            return {"ok": False, "message": f"unknown plan in {scope}: {plan_name}"}

        starters = set(discover_plan_starters(self.shared_path, plan_name, plan_scope=scope))
        if starter_file and starter_file not in starters:
            return {"ok": False, "message": f"invalid starter_file for {plan_name}: {starter_file}"}

        try:
            cfg_obj = json.loads(config_json) if config_json.strip() else {}
            if not isinstance(cfg_obj, dict):
                raise ValueError("config JSON must be an object")
        except Exception as e:
            return {"ok": False, "message": f"invalid config JSON: {e}"}

        # Fill missing keys from plan defaults so required placeholders are always resolvable.
        default_cfg = default_plan_config(self.shared_path, plan_name)
        if isinstance(default_cfg, dict):
            merged_cfg = dict(default_cfg)
            merged_cfg.update(cfg_obj)
            cfg_obj = merged_cfg

        try:
            cfg_obj = sanitize_config_object(cfg_obj)
        except Exception as e:
            return {"ok": False, "message": f"invalid config values: {e}"}
        if gateway_model_assignments is not None:
            encoded_assignments = json.dumps(gateway_model_assignments, separators=(",", ":"))
            if len(encoded_assignments) > 4000:
                return {"ok": False, "message": "plan has too many model assignments for one submission"}
            cfg_obj["GATEWAY_MODEL_ASSIGNMENTS"] = encoded_assignments
        elif str(cfg_obj.get("RUN_MODE") or "").lower() != "resume":
            # Only the gateway's assignment pass may create this internal field.
            cfg_obj.pop("GATEWAY_MODEL_ASSIGNMENTS", None)

        repo_path_clean = sanitize_text(repo_path, max_len=800, single_line=True) if str(repo_path).strip() else ""
        try:
            repo_url_clean = normalize_github_url(repo_url) if str(repo_url).strip() else ""
        except Exception as e:
            return {"ok": False, "message": f"invalid repo_url: {e}"}
        claimed_clean = sanitize_text(claimed_behavior, max_len=8000, single_line=True) if str(claimed_behavior).strip() else ""
        inline_clean = sanitize_text(inline_query_text, max_len=16000, single_line=False) if str(inline_query_text).strip() else ""

        auto_repo_path = ""
        if plan_name == "github_analyzer" and not repo_path_clean and repo_url_clean:
            try:
                auto_repo_path, repo_url_clean = self._gpu_find_existing_repo_checkout(repo_url_clean)
            except Exception:
                auto_repo_path = ""

        if repo_path_clean:
            cfg_obj["REPO_PATH"] = repo_path_clean
        if repo_url_clean:
            cfg_obj["REPO_URL"] = repo_url_clean
            if plan_name == "github_analyzer" and not repo_path_clean and not auto_repo_path:
                cfg_obj["REPO_PATH"] = ""
        if auto_repo_path:
            cfg_obj["REPO_PATH"] = auto_repo_path
        if claimed_clean:
            cfg_obj["CLAIMED_BEHAVIOR"] = claimed_clean
        if inline_clean:
            inline_path = write_inline_input_file(self.shared_path, plan_name, inline_clean, plan_scope=scope)
            chosen_key = next((k for k in INLINE_FILE_KEYS if k in cfg_obj), "QUERY_FILE")
            cfg_obj[chosen_key] = inline_path

        # github_analyzer resolves REPO_URL by cloning when REPO_PATH is empty.
        # Preflight this dependency so dashboard users get an immediate actionable error.
        if plan_name == "github_analyzer":
            repo_path_cfg = str(cfg_obj.get("REPO_PATH", "") or "").strip()
            repo_url_cfg = str(cfg_obj.get("REPO_URL", "") or "").strip()
            if repo_url_cfg and not repo_path_cfg:
                has_git, detail = self._gpu_has_git()
                if not has_git:
                    return {
                        "ok": False,
                        "message": (
                            "Cannot submit github_analyzer with REPO_URL because git is missing on GPU host. "
                            "Provide REPO_PATH to a local checkout, or install git on gpu host."
                        ),
                        "stderr": _clip_stdio(detail),
                    }

        starter_arg = starter_file.strip()
        out = orchestration_action("submit_prepared", {
            "plan_name": plan_name,
            "plan_scope": scope,
            "starter_file": starter_arg,
            "config": cfg_obj,
        })
        display_starter = starter_arg or "plan.md"
        out["message"] = (
            f"Submitted {scope}/{plan_name} ({display_starter})"
            if out.get("ok")
            else f"Failed to submit {scope}/{plan_name} ({display_starter})"
        )
        if out.get("ok") and plan_name == "github_analyzer":
            resolved_repo = str(cfg_obj.get("REPO_PATH", "") or "").strip()
            if resolved_repo:
                out["message"] += f" [local repo: {resolved_repo}]"
        return out


    def _resume_plan(self, batch_id: str) -> dict[str, Any]:
        if not batch_id:
            return {"ok": False, "message": "batch_id is required"}
        if not BATCH_ID_RE.fullmatch(batch_id):
            return {"ok": False, "message": f"invalid batch_id: {batch_id!r}"}

        brain = load_brain_state(self.shared_path)
        active = brain.get("active_batches", {}) if isinstance(brain.get("active_batches"), dict) else {}
        meta = active.get(batch_id) if isinstance(active.get(batch_id), dict) else {}
        if not meta:
            return {"ok": False, "message": f"batch_id not found in active_batches: {batch_id}"}

        plan_name = str(meta.get("plan") or "").strip()
        if not plan_name:
            plan_dir = str(meta.get("plan_dir") or "").strip()
            if plan_dir:
                plan_name = Path(plan_dir).name
        if not plan_name:
            return {"ok": False, "message": f"could not resolve plan name for batch {batch_id}"}

        cfg = meta.get("config") if isinstance(meta.get("config"), dict) else {}
        cfg = dict(cfg)
        cfg["RUN_MODE"] = "resume"
        cfg["BATCH_ID"] = batch_id
        cfg_json = json.dumps(cfg)

        plan_scope = "shoulders"
        plan_dir = str(meta.get("plan_dir") or "").strip()
        if "/plans/arms/" in plan_dir:
            plan_scope = "arms"

        out = self._start_plan(plan_name, cfg_json, plan_scope=plan_scope)
        if out.get("ok"):
            out["message"] = f"Resumed batch {batch_id} ({plan_scope}/{plan_name})"
        else:
            out["message"] = f"Failed to resume batch {batch_id} ({plan_scope}/{plan_name})"
        return out
