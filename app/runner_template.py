"""Runner script mounted into sandbox pods at /workspace/runner.py.

This file is read as a string by executor.py and injected into a ConfigMap.
It must be self-contained — no imports from the app package.
"""

RUNNER_SOURCE = r'''
import json
import inspect
import resource
import sys
import traceback

# ── Resource limits ────────────────────────────────────────────────
try:
    resource.setrlimit(resource.RLIMIT_NPROC, (64, 64))
except (ValueError, resource.error):
    pass  # may not be supported on all platforms

try:
    ten_mb = 10 * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_FSIZE, (ten_mb, ten_mb))
except (ValueError, resource.error):
    pass

try:
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
except (ValueError, resource.error):
    pass

# ── Discover and run tests ────────────────────────────────────────

def truncate(s, max_len=500):
    s = str(s)
    if len(s) > max_len:
        return s[:max_len] + "...[truncated]"
    return s


def main():
    results = []
    passed = 0
    failed = 0

    # Import solution first to catch syntax/import errors early
    try:
        import solution  # noqa: F401
    except Exception:
        tb = truncate(traceback.format_exc())
        print(f"Failed to import solution: {tb}", file=sys.stderr)
        result = {"tests": [], "passed": 0, "failed": 0, "error": f"solution import failed: {tb}"}
        print(f"__RESULT__ {json.dumps(result)}")
        sys.exit(2)

    # Import tests module
    try:
        import tests as test_module
    except Exception:
        tb = truncate(traceback.format_exc())
        print(f"Failed to import tests: {tb}", file=sys.stderr)
        result = {"tests": [], "passed": 0, "failed": 0, "error": f"tests import failed: {tb}"}
        print(f"__RESULT__ {json.dumps(result)}")
        sys.exit(2)

    # Discover test_* functions
    try:
        test_functions = [
            (name, obj)
            for name, obj in inspect.getmembers(test_module, inspect.isfunction)
            if name.startswith("test_")
        ]
    except Exception:
        tb = truncate(traceback.format_exc())
        print(f"Failed to collect tests: {tb}", file=sys.stderr)
        result = {"tests": [], "passed": 0, "failed": 0, "error": f"test collection failed: {tb}"}
        print(f"__RESULT__ {json.dumps(result)}")
        sys.exit(2)

    if not test_functions:
        result = {"tests": [], "passed": 0, "failed": 0, "error": "no test_* functions found"}
        print(f"__RESULT__ {json.dumps(result)}")
        sys.exit(2)

    # Run each test
    for name, func in test_functions:
        try:
            func()
            results.append({"name": name, "passed": True, "error": None})
            passed += 1
        except Exception:
            tb = truncate(traceback.format_exc())
            results.append({"name": name, "passed": False, "error": tb})
            failed += 1

    result = {"tests": results, "passed": passed, "failed": failed}
    print(f"__RESULT__ {json.dumps(result)}")

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
'''
