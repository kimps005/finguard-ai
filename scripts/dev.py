"""Start the local FinGuard development stack with one command.

    .venv\\Scripts\\python.exe scripts/dev.py          (Windows)
    ./.venv/bin/python scripts/dev.py                 (Linux, macOS)

Steps: load .env -> start db/redis in Docker on loopback -> apply migrations ->
sync stock_master when empty -> run FastAPI (auto-reload) and Next.js dev together.
Ctrl+C stops both servers; db/redis keep running for the next start.
"""

import argparse
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import threading

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"
COMPOSE = ["docker", "compose", "-f", "docker-compose.yml", "-f", "docker-compose.dev.yml"]


def parse_env_file(path: Path) -> dict[str, str]:
    """Read KEY=VALUE lines. Comments, blank lines and malformed lines are ignored."""
    values = {}
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        if not key.isidentifier():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
    return values


def build_env(file_values: dict[str, str], base: dict[str, str]) -> dict[str, str]:
    """Variables already set in the shell win over .env, like docker compose."""
    env = {**file_values, **base}
    for pg, postgres in (("PGDATABASE", "POSTGRES_DB"), ("PGUSER", "POSTGRES_USER"),
                         ("PGPASSWORD", "POSTGRES_PASSWORD")):
        if not env.get(pg) and env.get(postgres):
            env[pg] = env[postgres]
    env.setdefault("PGHOST", "127.0.0.1")
    env.setdefault("PGPORT", "55432")
    env.setdefault("PGCONNECT_TIMEOUT", "5")
    env.setdefault("REDIS_URL", "redis://127.0.0.1:56379/0")
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def port_in_use(port: int) -> bool:
    with socket.socket() as sock:
        return sock.connect_ex(("127.0.0.1", port)) == 0


def step(message: str) -> None:
    print(f"\n==> {message}", flush=True)


def run(cmd: list[str], env: dict[str, str], cwd: Path = ROOT) -> int:
    return subprocess.run(cmd, cwd=cwd, env=env).returncode


def needs_npm_install() -> bool:
    marker = FRONTEND / "node_modules" / ".package-lock.json"
    lock = FRONTEND / "package-lock.json"
    return not marker.exists() or lock.stat().st_mtime > marker.stat().st_mtime


def stream(name: str, proc: subprocess.Popen) -> None:
    for line in proc.stdout:
        sys.stdout.write(f"[{name}] {line}")
        sys.stdout.flush()


def stop(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        # npm.cmd and uvicorn --reload both spawn children; kill the whole tree.
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def spawn(name: str, cmd: list[str], env: dict[str, str], cwd: Path) -> subprocess.Popen:
    options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" \
        else {"start_new_session": True}
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace", bufsize=1, **options)
    threading.Thread(target=stream, args=(name, proc), daemon=True).start()
    return proc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the FinGuard dev stack.")
    parser.add_argument("--api-port", type=int, default=8000)
    parser.add_argument("--web-port", type=int, default=3000)
    parser.add_argument("--no-infra", action="store_true", help="skip starting db/redis in Docker")
    parser.add_argument("--sync", action="store_true", help="refresh stock_master even if populated")
    parser.add_argument("--no-reload", action="store_true", help="disable backend auto-reload")
    parser.add_argument("--api-only", action="store_true", help="run only the FastAPI server")
    args = parser.parse_args(argv)

    env_file = ROOT / ".env"
    if not env_file.exists():
        print("Missing .env. Copy .env.example to .env and fill in the keys.", file=sys.stderr)
        return 2
    env = build_env(parse_env_file(env_file), dict(os.environ))
    env["BACKEND_INTERNAL_URL"] = f"http://127.0.0.1:{args.api_port}"
    if ".venv" not in Path(sys.executable).parts:
        print(f"warning: not running from .venv ({sys.executable})", file=sys.stderr)

    busy = [p for p in (args.api_port, None if args.api_only else args.web_port) if p and port_in_use(p)]
    if busy:
        print(f"Port {', '.join(map(str, busy))} is already in use. Stop that process "
              "(e.g. `docker compose stop frontend`) or pass --api-port/--web-port.", file=sys.stderr)
        return 2

    if not args.no_infra:
        step("Starting db and redis")
        if run([*COMPOSE, "up", "-d", "--wait", "db", "redis"], env):
            return 1

    step("Applying migrations")
    if run([sys.executable, "-m", "backend.dart_etl", "--migrate"], env):
        return 1

    if env.get("DART_API_KEY") and env.get("FSC_SERVICE_KEY"):
        populated = run([sys.executable, "-m", "backend.sync_stock_master", "--check-populated"], env) == 0
        if args.sync or not populated:
            step("Syncing stock_master")
            if run([sys.executable, "-m", "backend.sync_stock_master"], env):
                print("warning: stock_master sync failed; continuing", file=sys.stderr)
    else:
        print("warning: DART_API_KEY and FSC_SERVICE_KEY are needed to sync stock_master", file=sys.stderr)

    npm = shutil.which("npm")
    if not args.api_only:
        if not npm:
            print("npm not found. Install Node.js 22.18+.", file=sys.stderr)
            return 2
        if needs_npm_install():
            step("Installing frontend dependencies")
            if run([npm, "ci"], env, FRONTEND):
                return 1

    step(f"API http://127.0.0.1:{args.api_port}" +
         ("" if args.api_only else f"   Web http://127.0.0.1:{args.web_port}") + "   (Ctrl+C to stop)")
    api_cmd = [sys.executable, "-m", "backend.server", "--port", str(args.api_port)]
    procs = [spawn("api", api_cmd if args.no_reload else [*api_cmd, "--reload"], env, ROOT)]
    if not args.api_only:
        procs.append(spawn("web", [npm, "run", "dev", "--", "--hostname", "127.0.0.1",
                                   "--port", str(args.web_port)], env, FRONTEND))

    exit_code = 0
    try:
        while all(p.poll() is None for p in procs):
            try:
                procs[0].wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                pass
        exited = next(p for p in procs if p.poll() is not None)
        exit_code = exited.returncode or 1
        print(f"\nA server exited with code {exited.returncode}; stopping the rest.", file=sys.stderr)
    except KeyboardInterrupt:
        print("\nStopping...", flush=True)
    finally:
        for proc in procs:
            stop(proc)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
