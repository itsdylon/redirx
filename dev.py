#!/usr/bin/env python3
"""
Redirx Dev Server - starts all services with one command.

Usage:
    python dev.py              Start everything (frontend + backend + worker + mock sites + stripe)
    python dev.py --no-mocks   Skip mock test sites
    python dev.py --no-stripe  Skip Stripe CLI webhook forwarding
    python dev.py --backend    Backend + worker only (no frontend, no mocks)

Services started:
    Frontend   http://localhost:3000   Vite dev server (proxies /api to Flask)
    Backend    http://localhost:5001   Flask API server
    Worker     (background)           Polls DB for pending jobs
    Stripe     (background)           Forwards Stripe webhooks to local backend
    Old Site   http://localhost:8000   Mock old site for testing
    New Site   http://localhost:8001   Mock new site for testing

Press Ctrl+C to stop all services.
"""

import subprocess
import sys
import os
import signal
import time
import threading

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
FRONTEND_DIR = os.path.join(PROJECT_ROOT, "frontend")
PYTHON = sys.executable

# Force UTF-8 for Python subprocesses on Windows (prevents cp1252 UnicodeEncodeError)
SUBPROCESS_ENV = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}

# ANSI colors for labeling output
COLORS = {
    "frontend": "\033[36m",   # cyan
    "backend":  "\033[33m",   # yellow
    "worker":   "\033[35m",   # magenta
    "stripe":   "\033[34m",   # blue
    "old-site": "\033[32m",   # green
    "new-site": "\033[32m",   # green
    "system":   "\033[90m",   # gray
    "error":    "\033[31m",   # red
    "reset":    "\033[0m",
}


def check_dependencies():
    """Check that critical Python packages are installed."""
    missing = []
    for mod in ["flask", "flask_cors", "openai", "supabase", "aiohttp", "bs4", "dotenv"]:
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)

    if missing:
        print(f"{COLORS['error']}[  error]{COLORS['reset']} Missing Python packages: {', '.join(missing)}")
        print(f"{COLORS['error']}[  error]{COLORS['reset']} Run: pip install -r requirements.txt")
        print(f"{COLORS['system']}         Using Python: {PYTHON}{COLORS['reset']}")
        print()
        return False
    return True


def prefix_output(process, name, color):
    """Read process output line by line and print with a colored prefix."""
    prefix = f"{color}[{name:>8}]{COLORS['reset']} "
    try:
        for line in iter(process.stdout.readline, ""):
            if line:
                print(f"{prefix}{line}", end="", flush=True)
    except (ValueError, OSError):
        pass


def find_npm():
    """Find npm executable, preferring npm.cmd on Windows."""
    if sys.platform == "win32":
        for cmd in ["npm.cmd", "npm"]:
            try:
                subprocess.run(
                    [cmd, "--version"],
                    capture_output=True, timeout=5, shell=True
                )
                return cmd
            except (subprocess.SubprocessError, FileNotFoundError):
                continue
    return "npm"


def find_stripe_cli():
    """Check if the Stripe CLI is installed and available."""
    for cmd in (["stripe.exe", "stripe"] if sys.platform == "win32" else ["stripe"]):
        try:
            result = subprocess.run(
                [cmd, "--version"],
                capture_output=True, timeout=5,
            )
            if result.returncode == 0:
                return cmd
        except (subprocess.SubprocessError, FileNotFoundError):
            continue
    return None


def start_owned_process(cmd, **kwargs):
    """Create only our own process group; never borrow the invoking shell's."""
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(cmd, **kwargs)


def stop_owned_processes(processes, grace_seconds=3):
    """Stop tracked children and their POSIX descendants, never name matches.

    POSIX groups are exactly the PIDs allocated by start_owned_process. Calling
    getpgid on an unisolated child could instead terminate the user's shell.
    Windows retains direct-child termination; tree cleanup is not verified here.
    """
    groups = []
    for _name, proc in processes:
        if os.name != "nt":
            groups.append(proc.pid)
        try:
            if os.name == "nt":
                if proc.poll() is None: proc.terminate()
            else:
                os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + grace_seconds
    for _name, proc in processes:
        try:
            proc.wait(timeout=max(0, deadline-time.monotonic()))
        except subprocess.TimeoutExpired:
            pass
    # A launcher may exit before its children. Terminate the recorded group,
    # not just the launcher PID, so npm/reloader descendants cannot leak.
    if os.name != "nt":
        for group in groups:
            try: os.killpg(group, signal.SIGKILL)
            except ProcessLookupError: pass
    for _name, proc in processes:
        if proc.poll() is None: proc.kill()
        proc.wait()


def main():
    args = sys.argv[1:]
    skip_mocks = "--no-mocks" in args
    skip_stripe = "--no-stripe" in args
    backend_only = "--backend" in args

    # Enable ANSI colors on Windows
    if sys.platform == "win32":
        os.system("")

    print(f"{COLORS['system']}{'=' * 60}")
    print("  Redirx Dev Server")
    print(f"{'=' * 60}{COLORS['reset']}")
    print()

    # Never stop pre-existing services: concurrent worktrees own their processes.

    # Pre-flight check
    if not check_dependencies():
        sys.exit(1)

    processes = []
    threads = []
    exited = set()  # Track processes that already reported exit

    def start(name, cmd, cwd=PROJECT_ROOT, shell=False):
        """Start a subprocess with output piping."""
        color = COLORS.get(name, COLORS["system"])
        print(f"{color}[{name:>8}]{COLORS['reset']} Starting: {' '.join(cmd) if isinstance(cmd, list) else cmd}")

        proc = start_owned_process(
            cmd,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            shell=shell,
            encoding="utf-8",
            errors="replace",
            env=SUBPROCESS_ENV,
        )
        processes.append((name, proc))

        t = threading.Thread(target=prefix_output, args=(proc, name, color), daemon=True)
        t.start()
        threads.append(t)
        return proc

    def shutdown_signal(_signum, _frame):
        raise KeyboardInterrupt

    previous_term = signal.signal(signal.SIGTERM, shutdown_signal)
    try:
        # 1. Mock sites (unless skipped) — served from external dirs
        if not skip_mocks and not backend_only:
            old_site_dir = os.path.join(os.path.dirname(PROJECT_ROOT), "mock-old-site")
            new_site_dir = os.path.join(os.path.dirname(PROJECT_ROOT), "mock-new-site")

            if os.path.isdir(old_site_dir) and os.path.isdir(new_site_dir):
                start("old-site", [PYTHON, "-m", "http.server", "8000", "--directory", old_site_dir])
                start("new-site", [PYTHON, "-m", "http.server", "8001", "--directory", new_site_dir])
                time.sleep(0.5)
            else:
                print(f"{COLORS['error']}[  error]{COLORS['reset']} Mock site dirs not found:")
                print(f"           Expected: {old_site_dir}")
                print(f"           Expected: {new_site_dir}")
                print(f"           Use --no-mocks to skip, or create the directories.")

        # 2. Backend API
        backend = start("backend", [PYTHON, "-m", "backend.app"])
        time.sleep(1)
        if backend.poll() is not None:
            print("Backend exited during startup; no worker was started.")
            return backend.returncode or 1

        # 3. Worker
        start("worker", [PYTHON, "-m", "backend.worker"])

        # 4. Stripe CLI webhook forwarding (unless skipped)
        if not skip_stripe:
            stripe_cmd = find_stripe_cli()
            if stripe_cmd:
                start("stripe", [
                    stripe_cmd, "listen",
                    "--forward-to", "http://localhost:5001/api/billing/webhook",
                ])
            else:
                print(f"{COLORS['error']}[  error]{COLORS['reset']} Stripe CLI not found. Webhooks won't be forwarded locally.")
                print(f"{COLORS['system']}         Install: https://docs.stripe.com/stripe-cli")
                print(f"         Or use --no-stripe to skip.{COLORS['reset']}")
                print()

        # 5. Frontend (unless backend-only)
        if not backend_only:
            npm = find_npm()
            start("frontend", [npm, "run", "dev", "--", "--strictPort"], cwd=FRONTEND_DIR, shell=sys.platform == "win32")

        print()
        print(f"{COLORS['system']}{'=' * 60}")
        if not backend_only:
            print("  Frontend   http://localhost:3000")
        print("  Backend    http://localhost:5001")
        if not skip_stripe and find_stripe_cli():
            print("  Stripe     Webhooks → http://localhost:5001/api/billing/webhook")
        if not skip_mocks and not backend_only:
            print("  Old Site   http://localhost:8000")
            print("  New Site   http://localhost:8001")
        print()
        print("  Press Ctrl+C to stop all services")
        print(f"{'=' * 60}{COLORS['reset']}")
        print()

        # Wait — only report each exit once
        while True:
            for name, proc in processes:
                if name not in exited:
                    ret = proc.poll()
                    if ret is not None:
                        exited.add(name)
                        color = COLORS.get(name, COLORS["system"])
                        if ret != 0:
                            print(f"{color}[{name:>8}]{COLORS['reset']} Exited with code {ret}")
                        if name in {"backend", "worker", "frontend"}:
                            return ret or 1
            time.sleep(1)

    except KeyboardInterrupt:
        print(f"\n{COLORS['system']}Shutting down all services...{COLORS['reset']}")

    finally:
        previous_int = signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        try:
            stop_owned_processes(processes)
        finally:
            signal.signal(signal.SIGINT, previous_int)
            signal.signal(signal.SIGTERM, previous_term)
        print(f"{COLORS['system']}Started services stopped; pre-existing services were left alone.{COLORS['reset']}")



if __name__ == "__main__":
    sys.exit(main() or 0)
