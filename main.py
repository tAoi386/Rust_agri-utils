"""Desktop entry point. launch.bat also configures the local Qt dependencies."""
import os
import sys
from pathlib import Path

if not getattr(sys, "frozen", False):
    deps = Path(__file__).resolve().parent / ".deps"
    if deps.exists():
        sys.path.insert(0, str(deps))

if __name__ == "__main__":
    if '--route-worker' in sys.argv[1:]:
        from rust_breeder.planner_process import worker_main
        raise SystemExit(worker_main())
    from rust_breeder.diagnostics import RuntimeDiagnostics
    base = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    diagnostics = RuntimeDiagnostics(base)
    from rust_breeder.app import run
    diagnostics.install_qt_handler()
    exit_code = run()
    diagnostics.record_exit(exit_code)
    raise SystemExit(exit_code)
