#!/usr/bin/env python3
"""Idempotently merge a baseline auto-mode safety ruleset into the GLOBAL
~/.claude/settings.json for every project registered in SQUEEZER_HOME/config.json.

Auto-mode config is global-only (verified empirically: a project-local
.claude/settings.json `autoMode` block is NOT read by `claude auto-mode
config`). This script is the fallback described in the design plan: it writes
a per-project fragment into the shared global file rather than requiring you
to hand-edit it, and re-running it is always safe (existing entries are never
duplicated).

This gives a *baseline* only. Command-pattern matching (Bash(...) globs) can't
fully capture "push to main vs. a feature branch" nuance — review each
project's rules with `claude auto-mode critique` after this runs.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config as _config  # noqa: E402

GLOBAL_SETTINGS = Path.home() / ".claude" / "settings.json"


def baseline_rules(path: str):
    return [
        f"Bash(git push --force*) in {path}",
        f"Bash(git push -f*) in {path}",
        f"Bash(git push origin main) in {path}",
        f"Bash(git push origin main:*) in {path}",
        f"Bash(git push origin master) in {path}",
        f"Bash(git push origin master:*) in {path}",
        f"Bash(rm -rf*) in {path}",
        f"Bash(docker compose*prod*) in {path}",
        f"Bash(docker-compose*prod*) in {path}",
        f"Bash(ansible-playbook*) in {path}",
        f"Read({path}/.env) in {path}",
        f"Write({path}/.env) in {path}",
        f"Edit({path}/.env) in {path}",
    ]


def squeezer_home_rules(squeezer_home: str):
    return [
        f"Write({squeezer_home}/state/elevation.json) in {squeezer_home}",
        f"Edit({squeezer_home}/state/elevation.json) in {squeezer_home}",
        f"Write({squeezer_home}/state/totp.json) in {squeezer_home}",
        f"Edit({squeezer_home}/state/totp.json) in {squeezer_home}",
        f"Read({squeezer_home}/.env) in {squeezer_home}",
        f"Write({squeezer_home}/.env) in {squeezer_home}",
        f"Edit({squeezer_home}/.env) in {squeezer_home}",
    ]


def load_settings():
    if not GLOBAL_SETTINGS.exists():
        return {}
    with open(GLOBAL_SETTINGS) as f:
        return json.load(f)


def save_settings(data):
    GLOBAL_SETTINGS.write_text(json.dumps(data, indent=2) + "\n")


def sync(config_path: Path):
    with open(config_path) as f:
        squeezer_config = json.load(f)

    settings = load_settings()
    auto_mode = settings.setdefault(
        "autoMode", {"environment": [], "allow": [], "soft_deny": [], "hard_deny": []}
    )
    for key in ("environment", "allow", "soft_deny", "hard_deny"):
        auto_mode.setdefault(key, [])

    changed = False
    for project in squeezer_config.get("projects", []):
        name = project["name"]
        path = project["path"]
        marker = f"### Project: {name} (managed by squeezer, path: {path})"

        if marker not in auto_mode["environment"]:
            auto_mode["environment"].append(marker)
            changed = True

        for rule in baseline_rules(path):
            if rule not in auto_mode["hard_deny"]:
                auto_mode["hard_deny"].append(rule)
                changed = True

    squeezer_home_str = str(_config.squeezer_home())
    for rule in squeezer_home_rules(squeezer_home_str):
        if rule not in auto_mode["hard_deny"]:
            auto_mode["hard_deny"].append(rule)
            changed = True

    if changed:
        save_settings(settings)
        print(f"Updated {GLOBAL_SETTINGS} with baseline auto-mode rules.")
    else:
        print("Auto-mode rules already up to date — nothing to change.")


if __name__ == "__main__":
    config_path = Path(sys.argv[1]) if len(sys.argv) > 1 else _config.config_path()
    if not config_path.exists():
        print(f"error: {config_path} not found — copy templates/config.example.json first", file=sys.stderr)
        sys.exit(1)
    sync(config_path)
