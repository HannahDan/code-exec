"""Runner mounted into sandbox pods at /workspace/runner.py.

executor.py reads this file verbatim into a ConfigMap, so it must stay
self-contained: stdlib only, no imports from the app package.
"""

import inspect
import json
import resource
import sys
import traceback

MAX_ERROR_CHARS = 500


def set_limits():
    limits = [
        (resource.RLIMIT_NPROC, 64),
        (resource.RLIMIT_FSIZE, 10 * 1024 * 1024),
        (resource.RLIMIT_CORE, 0),
    ]
    for which, value in limits:
        try:
            resource.setrlimit(which, (value, value))
        except (ValueError, OSError):
            pass


def truncate(s, max_len=MAX_ERROR_CHARS):
    s = str(s)
    return s if len(s) <= max_len else s[:max_len] + "...[truncated]"


def emit(result, code):
    sys.stdout.flush()
    print("__RESULT__ " + json.dumps(result), flush=True)
    sys.exit(code)


def collection_error(stage):
    tb = truncate(traceback.format_exc())
    print(f"{stage} failed:\n{tb}", file=sys.stderr, flush=True)
    emit({"tests": [], "passed": 0, "failed": 0, "error": f"{stage} failed: {tb}"}, 2)


def main():
    set_limits()

    try:
        import solution  # noqa: F401
    except BaseException:
        collection_error("solution import")

    try:
        import tests as test_module
    except BaseException:
        collection_error("tests import")

    try:
        test_functions = [
            (name, fn)
            for name, fn in inspect.getmembers(test_module, inspect.isfunction)
            if name.startswith("test_")
        ]
    except BaseException:
        collection_error("test collection")

    if not test_functions:
        emit({"tests": [], "passed": 0, "failed": 0, "error": "no test_* functions found"}, 2)

    results = []
    for name, fn in test_functions:
        try:
            fn()
            results.append({"name": name, "passed": True, "error": None})
        except BaseException as exc:
            if isinstance(exc, KeyboardInterrupt):
                raise
            message = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
            results.append({"name": name, "passed": False, "error": truncate(message)})

    passed = sum(1 for r in results if r["passed"])
    failed = len(results) - passed
    emit({"tests": results, "passed": passed, "failed": failed}, 0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
