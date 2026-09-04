"""QR-code PNG generation for /squeezer:2fa-setup's enrollment step — the
only place in squeezer that needs a third-party image library. totp.py
stays stdlib-only by design (see its own docstring); this one step needs
the `qrcode` package (with its Pillow-based PNG backend), so that
dependency is isolated here. Unlike check_mcp_deps.py's `mcp` install (which
blocks a daemon capability if missing), a failed install here just means
the setup command falls back to text-only enrollment, so there's no
ensurepip/get-pip bootstrap — pip is assumed already usable on the
interpreter running the command."""
import subprocess

REQUIRED_SPEC = "qrcode[pil]"


def is_importable(python_bin: str) -> bool:
    result = subprocess.run(
        [python_bin, "-c", "import qrcode, PIL"],
        capture_output=True, text=True,
    )
    return result.returncode == 0


def is_venv_interpreter(python_bin: str) -> bool:
    """True if python_bin is a virtualenv/venv interpreter. Those already have
    an isolated, writable site-packages, and pip actively refuses `--user`
    there — so the --user flag below must be conditional, not assumed."""
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


def install_qrcode(python_bin: str) -> dict:
    """Installs REQUIRED_SPEC into python_bin's site-packages (its own, if
    it's a venv interpreter; otherwise --user). Returns {"ok": bool, "error":
    str|None}. Falls back to --break-system-packages only on PEP 668
    "externally-managed-environment" interpreters."""
    result = _run_pip_install(python_bin, [REQUIRED_SPEC])
    if result.returncode == 0:
        return {"ok": True, "error": None}

    if "externally-managed-environment" in result.stderr:
        result = _run_pip_install(python_bin, ["--break-system-packages", REQUIRED_SPEC])
        if result.returncode == 0:
            return {"ok": True, "error": None}

    return {"ok": False, "error": result.stderr[-2000:]}


def ensure_qrcode_installed(python_bin: str) -> dict:
    """Check-then-fix. Returns {"ok": bool, "already_satisfied": bool,
    "error": str|None}."""
    if is_importable(python_bin):
        return {"ok": True, "already_satisfied": True, "error": None}

    install_result = install_qrcode(python_bin)
    if not install_result["ok"]:
        return {"ok": False, "already_satisfied": False, "error": install_result["error"]}

    if not is_importable(python_bin):
        return {
            "ok": False, "already_satisfied": False,
            "error": "installed qrcode[pil] but 'import qrcode, PIL' still fails",
        }

    return {"ok": True, "already_satisfied": False, "error": None}


def generate_qr_png(data: str, output_path: str, box_size: int = 10, border: int = 4) -> None:
    """Renders `data` (an otpauth:// URI, typically) as a QR-code PNG at
    output_path. Raises ImportError if qrcode/PIL aren't importable —
    callers should call ensure_qrcode_installed first."""
    import qrcode

    img = qrcode.make(data, box_size=box_size, border=border)
    img.save(output_path)
