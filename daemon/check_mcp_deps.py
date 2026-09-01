#!/usr/bin/env python3
"""Ensures the `mcp` Python package (specifically mcp.server.fastmcp.FastMCP,
the API mcp/telegram_server.py imports) is importable by a given Python
interpreter — the one install_service.py bakes into the daemon service's
PATH (see its build_service_path docstring for why that interpreter, not
whatever `python3` happens to resolve first, is the one that matters).

Without this, the squeezer-telegram MCP server fails to start silently: a
spawned `claude -p` turn has no `telegram_send` tool to reply with, so a
direct human Telegram message gets queued and answered by nothing — no
error surfaces anywhere the human would see it. Invoked by the
`/squeezer:setup` command, before daemon installation; safe to re-run.
"""
import subprocess
import sys
import tempfile
import urllib.request

# mcp 2.x renamed FastMCP -> MCPServer; telegram_server.py targets the v1 API.
REQUIRED_SPEC = "mcp[cli]<2"
GET_PIP_URL = "https://bootstrap.pypa.io/get-pip.py"


def is_importable(python_bin: str) -> bool:
    result = subprocess.run(
        [python_bin, "-c", "from mcp.server.fastmcp import FastMCP"],
        capture_output=True, text=True,
    )
    return result.returncode == 0


def is_venv_interpreter(python_bin: str) -> bool:
    """True if python_bin is a virtualenv/venv interpreter. Those already have
    an isolated, writable site-packages, and pip actively refuses `--user`
    there ("User site-packages are not visible in this virtualenv") — so the
    --user flag below must be conditional, not assumed."""
    result = subprocess.run(
        [python_bin, "-c", "import sys; print(sys.prefix != sys.base_prefix)"],
        capture_output=True, text=True,
    )
    return result.returncode == 0 and result.stdout.strip() == "True"


def _run_pip_install(python_bin: str, args: list[str]) -> subprocess.CompletedProcess:
    user_flag = [] if is_venv_interpreter(python_bin) else ["--user"]
    return subprocess.run(
        [python_bin, "-m", "pip", "install", *user_flag, *args],
        capture_output=True, text=True,
    )


def install_mcp(python_bin: str) -> dict:
    """Installs REQUIRED_SPEC into python_bin's site-packages (its own, if
    it's a venv interpreter; otherwise --user). Returns {"ok": bool, "error":
    str|None}. Falls back to --break-system-packages only on PEP 668
    "externally-managed-environment" interpreters (the Debian/Ubuntu system
    Python default) — safe here since a venv install stays inside the venv
    and a --user install stays out of the system site-packages either way."""
    result = _run_pip_install(python_bin, [REQUIRED_SPEC])
    if result.returncode == 0:
        return {"ok": True, "error": None}

    if "externally-managed-environment" in result.stderr:
        result = _run_pip_install(python_bin, ["--break-system-packages", REQUIRED_SPEC])
        if result.returncode == 0:
            return {"ok": True, "error": None}

    return {"ok": False, "error": result.stderr[-2000:]}


def ensure_pip(python_bin: str) -> dict:
    """Bootstraps pip for python_bin if `python_bin -m pip` isn't available at
    all (e.g. a minimal Debian python3 with no ensurepip/pip package).  Tries
    stdlib ensurepip first (no network), then falls back to downloading
    get-pip.py — network-dependent, same trust boundary as the Telegram API
    calls this same setup flow already makes elsewhere."""
    result = subprocess.run([python_bin, "-m", "pip", "--version"], capture_output=True, text=True)
    if result.returncode == 0:
        return {"ok": True, "error": None}

    user_flag = [] if is_venv_interpreter(python_bin) else ["--user"]

    result = subprocess.run([python_bin, "-m", "ensurepip", *user_flag], capture_output=True, text=True)
    if result.returncode == 0:
        return {"ok": True, "error": None}

    try:
        with tempfile.NamedTemporaryFile(suffix=".py", delete=False) as f:
            urllib.request.urlretrieve(GET_PIP_URL, f.name)
            get_pip_path = f.name
    except OSError as e:
        return {"ok": False, "error": f"could not download get-pip.py: {e}"}

    result = subprocess.run([python_bin, get_pip_path, *user_flag], capture_output=True, text=True)
    if result.returncode == 0:
        return {"ok": True, "error": None}

    if "externally-managed-environment" in result.stderr:
        result = subprocess.run(
            [python_bin, get_pip_path, *user_flag, "--break-system-packages"],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            return {"ok": True, "error": None}

    return {"ok": False, "error": result.stderr[-2000:]}


def ensure_mcp_installed(python_bin: str = None) -> dict:
    """Full check-then-fix. Returns {"ok": bool, "already_satisfied": bool,
    "error": str|None}."""
    python_bin = python_bin or sys.executable
    if is_importable(python_bin):
        return {"ok": True, "already_satisfied": True, "error": None}

    pip_result = ensure_pip(python_bin)
    if not pip_result["ok"]:
        return {"ok": False, "already_satisfied": False, "error": f"could not bootstrap pip: {pip_result['error']}"}

    install_result = install_mcp(python_bin)
    if not install_result["ok"]:
        return {"ok": False, "already_satisfied": False, "error": install_result["error"]}

    if not is_importable(python_bin):
        return {
            "ok": False, "already_satisfied": False,
            "error": "installed the mcp package but 'from mcp.server.fastmcp import FastMCP' still fails",
        }

    return {"ok": True, "already_satisfied": False, "error": None}


def main():
    python_bin = sys.argv[1] if len(sys.argv) > 1 else sys.executable
    result = ensure_mcp_installed(python_bin)
    if result["already_satisfied"]:
        print(f"ok: mcp package already importable by {python_bin}")
    elif result["ok"]:
        print(f"ok: installed {REQUIRED_SPEC} for {python_bin}")
    else:
        print(
            f"error: could not make the mcp package importable by {python_bin} — "
            f"the squeezer-telegram MCP server (telegram_send tool) will not work "
            f"until this is fixed manually:\n{result['error']}",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
