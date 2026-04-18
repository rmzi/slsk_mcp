"""One-time CLI bootstrap for Tidal OAuth.

Run on the host machine via ``slsk-mcp-tidal-login`` (console script) or
``uvx --from git+ssh://...slsk_mcp.git@<hash> slsk-mcp-tidal-login``.

Prints a pairing URL and short code; the user authorizes on tidal.com; the
resulting refresh token is saved to ``~/.config/slsk-mcp/tidal.json`` and
loaded silently on subsequent MCP startups.
"""

from __future__ import annotations

import sys

from .tidal import ensure_config_dir, get_session_path


def main() -> int:
    try:
        import tidalapi
    except ImportError:
        print(
            "tidalapi not installed. Run `uv sync` or `pip install -e .` in the project.",
            file=sys.stderr,
        )
        return 1

    ensure_config_dir()
    session_file = get_session_path()

    session = tidalapi.Session()
    print("Starting Tidal device-code login. Follow the URL printed below.", flush=True)
    try:
        session.login_oauth_simple()
    except Exception as exc:
        print(f"Login aborted: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if not session.check_login():
        print("Login did not complete successfully.", file=sys.stderr)
        return 1

    try:
        session.save_session_to_file(session_file)
    except Exception as exc:
        print(f"Failed to save session: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(f"Session saved to {session_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
