"""CLI entrypoint: `python -m api_framework` or `api-framework`."""

from __future__ import annotations

import argparse

import uvicorn

from api_framework.core.config import Environment, generate_secret_key, get_settings


def main() -> None:
    parser = argparse.ArgumentParser(prog="api-framework", description="Run the API server.")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="Bind port (default: 8000)")
    parser.add_argument("--reload", action="store_true", help="Auto-reload on code changes")
    parser.add_argument(
        "--generate-secret",
        action="store_true",
        help="Print a fresh APP_SECRET_KEY and exit",
    )
    args = parser.parse_args()

    if args.generate_secret:
        print(generate_secret_key())
        return

    settings = get_settings()

    uvicorn.run(
        "api_framework.app:app",
        host=args.host,
        port=args.port,
        # Reload only where it is safe; it forks a reloader process and is
        # never appropriate in a deployed environment.
        reload=args.reload and settings.env is Environment.LOCAL,
        # Our own logging config owns formatting - see core/logging.py.
        log_config=None,
        access_log=False,
    )


if __name__ == "__main__":
    main()
