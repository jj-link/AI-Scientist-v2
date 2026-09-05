"""Production entry point: python -m ai_scientist.ui."""
from __future__ import annotations

import argparse
from pathlib import Path
import socket
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Serve AI-Scientist Studio on http://127.0.0.1:8765 (local access only).")
    parser.add_argument("--development", action="store_true", help="Allow only the Vite development Origin http://127.0.0.1:5173; binding remains loopback-only.")
    args = parser.parse_args(argv)
    try:
        import uvicorn
        from .app import create_app
    except ImportError as exc:
        print(f"AI-Scientist Studio cannot start: missing dependency {exc.name!r}. Install requirements-ui.txt using this Python environment.", file=sys.stderr)
        return 1
    root = Path(__file__).resolve().parents[2]
    if not args.development and not (root / "frontend" / "dist" / "index.html").is_file():
        print("AI-Scientist Studio cannot start: built frontend assets are missing. Run npm ci and npm run build in frontend, then run this command again.", file=sys.stderr)
        return 1
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind(("127.0.0.1", 8765))
        listener.listen(128)
    except OSError as exc:
        listener.close()
        print(f"AI-Scientist Studio cannot bind to 127.0.0.1:8765. The port may already be in use or unavailable (OS error {exc.errno}).", file=sys.stderr)
        return 1
    try:
        app = create_app(root, development=args.development)
        config = uvicorn.Config(app, host="127.0.0.1", port=8765, proxy_headers=False, server_header=False, log_level="info")
        print("AI-Scientist Studio: http://127.0.0.1:8765", flush=True)
        uvicorn.Server(config).run(sockets=[listener])
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(f"AI-Scientist Studio could not start ({type(exc).__name__}). Check local configuration and filesystem permissions.", file=sys.stderr)
        return 1
    finally:
        listener.close()


if __name__ == "__main__":
    raise SystemExit(main())
