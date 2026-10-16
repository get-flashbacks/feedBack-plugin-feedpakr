"""Shared logic for the strengthened checks in custom-checks.yml (issue #84).

The workflow holds two kinds of checks:

* Structural checks, implemented here. They parse the actual sources —
  Python AST for routes.py, a scoped text window for screen.js, git diffs
  for the version/changelog policy — and have representative pass/fail
  cases in tests/test_custom_checks.py. They narrow what the old
  text-presence versions waved through; they still are not proof of the
  runtime behavior (static analysis cannot resolve aliases or
  dynamically-built registrations, and nothing here runs the host app).
* Heuristic text checks, left inline in the workflow and marked
  ``# Heuristic:`` there. Those grep for marker strings. Treat them as
  tripwires that catch the specific regression they were written for —
  passing one never demonstrates the advertised behavior.

Deliberate exemptions from the functional-change (version + changelog)
policy are documented on ``is_functional_path`` and
``manifest_change_is_functional``.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess

# ── blocking-handlers-use-def ────────────────────────────────────────────────

# Bare names that block when called directly: open()/sleep() (from-import
# style). A callable reached through an attribute is only blocking when its
# receiver is a known blocking module — matching the attribute name alone
# would flag asyncio.sleep() (the canonical non-blocking yield) and unrelated
# .open() methods on archives/files.
_BARE_BLOCKING = frozenset({"open", "sleep"})
# Modules where any attribute call blocks (subprocess.run, requests.get, ...).
_BLOCKING_ANY_ATTR = frozenset({"subprocess", "requests", "socket"})
# Modules where only specific attributes block — time.monotonic()/time.time()
# must stay allowed.
_BLOCKING_ATTRS = {
    "time": frozenset({"sleep"}),
    "os": frozenset({"open", "read", "write", "system", "popen", "wait", "waitpid"}),
}
_EXECUTOR_HINTS = ("run_in_executor", "to_thread")


def _is_executor_call(call: ast.Call) -> bool:
    fn = call.func
    if isinstance(fn, ast.Attribute):
        return fn.attr in _EXECUTOR_HINTS
    if isinstance(fn, ast.Name):
        return fn.id in _EXECUTOR_HINTS
    return False


def _blocking_marker(call: ast.Call) -> str | None:
    """The blocking name a call goes through, if any.

    A bare name (``open()``/``sleep()``) blocks. An attribute call blocks
    only when the receiver is a known blocking module — ``time.sleep``,
    ``subprocess.run``, ``requests.get`` — so ``asyncio.sleep``,
    ``time.monotonic`` and a ``.open()`` method on an archive are left
    alone. The previous check matched only the receiver name, so it missed
    ``time.sleep`` entirely.
    """
    fn = call.func
    if isinstance(fn, ast.Name):
        return fn.id if fn.id in _BARE_BLOCKING else None
    if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name):
        mod = fn.value.id
        if mod in _BLOCKING_ANY_ATTR or fn.attr in _BLOCKING_ATTRS.get(mod, ()):
            return f"{mod}.{fn.attr}"
    return None


def _executor_arg_names(call: ast.Call) -> set[str]:
    names: set[str] = set()
    for arg in list(call.args) + [kw.value for kw in call.keywords]:
        for node in ast.walk(arg):
            if isinstance(node, ast.Name):
                names.add(node.id)
    return names


def _calls_evaluated_now(node: ast.AST):
    """Yield calls in ``node`` that evaluate on the calling thread.

    Lambda bodies are deferred until the lambda is invoked (typically in the
    executor thread), so they are pruned rather than descended into.
    """
    if isinstance(node, ast.Lambda):
        return
    if isinstance(node, ast.Call):
        yield node
    for child in ast.iter_child_nodes(node):
        yield from _calls_evaluated_now(child)


def check_blocking_handlers(src: str, filename: str = "routes.py") -> list[str]:
    """Blocking calls an async route handler would run on the event loop.

    Structural rule (issue #84): rather than exempting an entire coroutine
    when the text ``run_in_executor``/``to_thread`` appears anywhere in it,
    every blocking call must itself sit either (a) inside an executor call
    that receives it as a deferred callable — ``run_in_executor(None, open)``,
    ``asyncio.to_thread(parse)`` — or (b) inside a nested def that the
    coroutine actually hands to an executor. A blocking call executed
    directly by the coroutine (or by an inline helper it calls) is reported
    even when the same coroutine also uses an executor elsewhere — including
    one evaluated as an executor *argument* (``to_thread(open(path))`` calls
    ``open`` on the loop; only the bare callable ``to_thread(open, path)`` is
    deferred).

    Known limits, same class as before: blocking work hidden behind an
    alias, a module-level helper, a lambda passed to the executor, or a
    nested def that is both handed to an executor and invoked inline stays
    invisible to this scan.
    """
    tree = ast.parse(src, filename=filename)
    errors: list[str] = []
    seen: set[tuple[int, str]] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        subtree = list(ast.walk(node))
        workers: set[str] = set()
        for sub in subtree:
            if isinstance(sub, ast.Call) and _is_executor_call(sub):
                workers |= _executor_arg_names(sub)
        _scan_function_body(
            node, f"async def {node.name}", workers, errors, seen,
        )
    errors.sort(key=lambda e: int(e.split(":", 1)[0].replace("line ", "")))
    return errors


def _scan_function_body(
    fn: ast.AST, context: str, workers: set[str], errors: list[str],
    seen: set[tuple[int, str]],
) -> None:
    """Scan one function body, recursing into inline nested defs that are
    not executor workers — those run synchronously on the event loop."""
    for child in ast.iter_child_nodes(fn):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if child.name in workers:
                continue  # handed to run_in_executor/to_thread — runs off-loop
            _scan_function_body(
                child, f"{context} -> inline def {child.name}", workers,
                errors, seen,
            )
            continue
        if isinstance(child, ast.Lambda):
            continue  # body does not execute until called; call sites are scanned
        if isinstance(child, ast.Call):
            if _is_executor_call(child):
                # The executor call's arguments are evaluated on the loop
                # before the worker runs; a Call among them (but not a bare
                # callable reference) executes here.
                for arg in list(child.args) + [kw.value for kw in child.keywords]:
                    for evaluated in _calls_evaluated_now(arg):
                        if _is_executor_call(evaluated):
                            continue
                        marker = _blocking_marker(evaluated)
                        if marker is not None and (evaluated.lineno, marker) not in seen:
                            seen.add((evaluated.lineno, marker))
                            errors.append(
                                f"line {evaluated.lineno}: {context} evaluates "
                                f"blocking {marker}(...) as an executor argument "
                                "— call it inside the worker instead"
                            )
                continue  # the executor worker itself runs in a thread
            marker = _blocking_marker(child)
            # An async def nested inside another async def is reachable
            # from both scans — report each call site once.
            if marker is not None and (child.lineno, marker) not in seen:
                seen.add((child.lineno, marker))
                errors.append(
                    f"line {child.lineno}: {context} runs blocking "
                    f"{marker}(...) directly on the event loop — wrap it in "
                    "run_in_executor/to_thread or move it into the nested def "
                    "passed to the executor"
                )
        _scan_function_body(child, context, workers, errors, seen)


# ── idempotent-top-level-guard ───────────────────────────────────────────────

_HOTPATH_RE = re.compile(r"(window\.addEventListener|document\.addEventListener|setInterval)\s*\(")
_GUARD_RE = re.compile(r"window\.__\w+")
# How far above a registration a window.__* guard flag may appear and still
# count as guarding it. A file-global guard reference anywhere (the old
# check) could be unrelated to the registration; proximity is what ties
# them together. This window size is a heuristic parameter, covered by the
# representative cases in tests/test_custom_checks.py.
_GUARD_LOOKBACK_LINES = 40


def check_reload_guard(js: str, filename: str = "screen.js") -> list[str]:
    """Top-level listeners/timers must sit under a nearby window.__* guard.

    scope (issue #84): the old check let any ``window.__`` reference
    anywhere in the file satisfy every registration site; this one requires
    a guard within ``_GUARD_LOOKBACK_LINES`` lines above each hit. The
    look-back is still text-based — a deliberately-decoupled guard would
    need real DOM execution to verify — but it now fails the "flag exists
    somewhere, unrelated to this listener" shape it previously passed.
    """
    errors: list[str] = []
    lines = js.split("\n")
    for i, line in enumerate(lines):
        if not _HOTPATH_RE.search(line):
            continue
        lo = max(0, i - _GUARD_LOOKBACK_LINES)
        if not any(_GUARD_RE.search(prev) for prev in lines[lo:i + 1]):
            errors.append(
                f"line {i + 1}: top-level addEventListener/setInterval has no "
                f"window.__* reload guard within {_GUARD_LOOKBACK_LINES} lines "
                "above it — screen.js re-executes on plugin reload"
            )
    return errors


# ── version-bumped-on-change / changelog-updated ─────────────────────────────

_FUNCTIONAL_RE = re.compile(r"\.(py|js|html|css)$")
_SCHEMA_JSON_RE = re.compile(r"(?:^|/)(?:assets/schemas/.+\.json|.+\.schema\.json)$")


def _normalize(path: str) -> str:
    p = path.replace("\\", "/").strip()
    while p.startswith("./"):
        p = p[2:]
    return p


def is_functional_path(path: str) -> bool:
    """Whether a changed path counts as a functional change for the
    version-bumped-on-change / changelog-updated policy.

    Counts: Python/JavaScript/HTML source, user-visible CSS, and schema
    JSON (``assets/schemas/*.json`` or any ``*.schema.json``).

    Deliberate, documented exemptions:

    * ``tests/``, ``test/`` — test-only changes ship no behavior.
    * ``.github/`` — CI config, including these check scripts themselves;
      extends the policy's pre-existing exemption of ``.github/*.yml``
      (workflow edits never required a bump), so CI-only files are treated
      consistently regardless of extension.
    * Docs and non-schema data (``*.md``, fixtures) — not shipped behavior.
    * ``plugin.json`` — handled by ``manifest_change_is_functional`` below:
      a version-only bump is exempt on its own, any other manifest field
      counts as functional.
    """
    p = _normalize(path)
    if any(p.startswith(prefix) for prefix in ("tests/", "test/", ".github/")):
        return False
    if p == "plugin.json":
        return False
    return bool(_FUNCTIONAL_RE.search(p) or _SCHEMA_JSON_RE.search(p))


def manifest_change_is_functional(old_text: str, new_text: str) -> bool:
    """True when plugin.json's fields other than ``version`` changed.

    Parses both revisions instead of scanning the diff, so a single-line or
    minified manifest cannot slip a field edit past a line-based version
    filter. An unparseable manifest counts as functional (the policy check
    then fails loudly rather than silently skipping).

    Deliberate, documented exemption: bumping the version is the policy's
    own required response, so a version-only change is not functional on
    its own — it lets an otherwise exempt PR (CI, docs) close out with its
    bump. Every other manifest edit (nav, screen, styles, minHost, ...) is
    functional and therefore requires that bump to be a real one; the
    version job still fails an unchanged version there.
    """
    try:
        old = json.loads(old_text) if old_text else {}
        new = json.loads(new_text) if new_text else {}
    except ValueError:
        return True
    for manifest in (old, new):
        if isinstance(manifest, dict):
            manifest.pop("version", None)
    return old != new


def _git(args: list[str], cwd: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, check=False,
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def functional_changes(base_sha: str, plugin_dir: str = ".") -> list[str]:
    """Functional paths changed since ``base_sha`` (as ``git diff``
    reports them), plus a marker entry when plugin.json changed beyond its
    version field. Empty list when git cannot diff — the jobs then skip,
    mirroring their no-usable-base behavior."""
    out = _git(["diff", "--name-only", base_sha, "HEAD", "--", "."], cwd=plugin_dir)
    if out is None:
        return []
    changed = [_normalize(p) for p in out.splitlines() if p.strip()]
    functional = [p for p in changed if is_functional_path(p)]
    if "plugin.json" in changed:
        old_text = _git(["show", f"{base_sha}:plugin.json"], cwd=plugin_dir)
        try:
            with open(os.path.join(plugin_dir, "plugin.json"), encoding="utf-8") as fh:
                new_text = fh.read()
        except OSError:
            new_text = ""
        if manifest_change_is_functional(old_text or "", new_text):
            functional.append('plugin.json (manifest fields beyond "version")')
    return functional


# ── new-modules-have-tests ───────────────────────────────────────────────────

def is_new_module(path: str) -> bool:
    """A newly-added top-level Python module that plugin tests should
    reference. The tests/ and .github/ exemptions documented on
    is_functional_path apply — those are not plugin modules."""
    p = _normalize(path)
    return p.endswith(".py") and is_functional_path(p)


def module_referenced_in(source: str, base: str) -> bool:
    """True when a test file's AST actually *uses* the module ``base`` —
    imports it, references it through an import alias, or passes the bare
    module-name string literal (how load_sibling-style test doubles name
    it). Prose such as ``# see feedpakr_foo.py`` does not count, and neither
    does a bare module-name string that is not passed to a call (e.g. a
    docstring): the old git-grep check let any mention anywhere satisfy the
    requirement (issue #84)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    names = {base}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == base:
                    return True
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module and node.module.split(".")[0] == base:
                return True
        elif isinstance(node, ast.Call):
            for arg in list(node.args) + [kw.value for kw in node.keywords]:
                if isinstance(arg, ast.Constant) and arg.value == base:
                    return True
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in names:
            return True
    return False


def _module_basename(path: str) -> str:
    return os.path.basename(_normalize(path))[:-3]


def _test_sources(plugin_dir: str) -> list[str]:
    sources: list[str] = []
    for tests_dir in ("tests", "test"):
        root = os.path.join(plugin_dir, tests_dir)
        if not os.path.isdir(root):
            continue
        for dirpath, _dirs, files in os.walk(root):
            for fn in sorted(files):
                if fn.endswith(".py"):
                    try:
                        with open(os.path.join(dirpath, fn), encoding="utf-8") as fh:
                            sources.append(fh.read())
                    except OSError:
                        continue
    return sources


def unreferenced_new_modules(base_sha: str, plugin_dir: str = ".") -> list[str]:
    """New top-level modules (vs ``base_sha``) that no test file references
    via import, alias, or the module-name string literal. Empty when git
    cannot diff, there are no new modules, or the repo has no tests
    directory — same skips as before, with a stronger reference bar."""
    out = _git(
        ["diff", "--name-only", "--diff-filter=A", base_sha, "HEAD", "--", "."],
        cwd=plugin_dir,
    )
    if out is None:
        return []
    new_modules = [
        p for p in (_normalize(x) for x in out.splitlines() if x.strip())
        if is_new_module(p)
    ]
    if not new_modules:
        return []
    sources = _test_sources(plugin_dir)
    if not sources:
        return []
    return [
        m for m in new_modules
        if not any(module_referenced_in(src, _module_basename(m)) for src in sources)
    ]
