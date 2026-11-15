"""Desktop entry point and packaged worker dispatch."""
from __future__ import annotations

import multiprocessing
import os
import sys
from pathlib import Path


def main() -> None:
    multiprocessing.freeze_support()
    # Frozen interpreters can ignore PYTHONIOENCODING inherited from the UI.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace", line_buffering=True)
    if getattr(sys, "frozen", False):
        os.chdir(Path(sys.executable).resolve().parent)
        tools = Path(sys._MEIPASS) / "tools"
        os.environ["PATH"] = str(tools) + os.pathsep + os.environ.get("PATH", "")
    if len(sys.argv) > 1 and sys.argv[1] == "--worker":
        worker = sys.argv[2]
        sys.argv = [worker, *sys.argv[3:]]
        if worker == "we_duplicate_finder_readonly.py":
            from we_duplicate_finder_readonly import main as run
        elif worker == "we_delisted_archiver.py":
            from we_delisted_archiver import main as run
        elif worker == "bulk_unsub_controller.py":
            from bulk_unsub_controller import main as run
        else:
            raise SystemExit(f"Unknown worker: {worker}")
        run()
        return

    from we_ui import main as run
    run()


if __name__ == "__main__":
    main()
