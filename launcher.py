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
        standard_library = str(Path(sys._MEIPASS) / "bootstrap" / "stdlib.zip")
        sys.path.insert(0, standard_library)
        for name, module in list(sys.modules.items()):
            if name.split(".")[0] in sys.stdlib_module_names and hasattr(module, "__path__"):
                module.__path__.append(standard_library + "/" + name.replace(".", "/"))
    if len(sys.argv) > 1 and sys.argv[1] == "--worker":
        worker = sys.argv[2]
        sys.argv = [worker, *sys.argv[3:]]
        if worker == "we_duplicate_finder_readonly.py":
            from semantic_runtime import activate_runtime
            activate_runtime()
            from we_duplicate_finder_readonly import main as run
        elif worker == "we_delisted_archiver.py":
            from we_delisted_archiver import main as run
        elif worker == "bulk_unsub_controller.py":
            from bulk_unsub_controller import main as run
        elif worker == "semantic_runtime.py":
            from semantic_runtime import main as run
        elif worker == "we_workshop_guard.py":
            from we_workshop_guard import main as run
        else:
            raise SystemExit(f"Unknown worker: {worker}")
        run()
        return

    if Path(sys.executable).stem.lower() == 'weworkshopguard' or '--workshop-tray' in sys.argv:
        if '--workshop-tray' in sys.argv:
            sys.argv.remove('--workshop-tray')
        from we_workshop_tray import main as run
        run()
        return

    from we_ui import main as run
    run()


if __name__ == "__main__":
    main()
