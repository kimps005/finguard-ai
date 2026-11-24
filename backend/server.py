"""Run the FinGuard API with the project's fixed Uvicorn settings.

Usage: python -m backend.server [--host 0.0.0.0] [--port 8000] [--reload]
"""

import argparse

import uvicorn


# Uvicorn calls a custom "module:attr" loop as the loop constructor. Async psycopg cannot use
# the Proactor loop that Uvicorn would otherwise pick on Windows.
LOOP = "asyncio:SelectorEventLoop"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true", help="restart when backend/ changes")
    args = parser.parse_args(argv)
    uvicorn.run(
        "backend.main:app",
        host=args.host,
        port=args.port,
        workers=1,  # One process owns the GPU semaphore and the loaded models.
        reload=args.reload,
        reload_dirs=["backend"] if args.reload else None,
        loop=LOOP,
    )


if __name__ == "__main__":
    main()
