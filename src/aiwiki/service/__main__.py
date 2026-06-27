"""Run the service: `AIWIKI_BUNDLE=... AIWIKI_TOKEN=... python -m aiwiki.service`."""
from __future__ import annotations

import os


def main() -> None:
    import uvicorn

    host = os.environ.get("AIWIKI_HOST", "127.0.0.1")
    port = int(os.environ.get("AIWIKI_PORT", "8787"))
    uvicorn.run("aiwiki.service.app:app", host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
