"""Representative cases for the strengthened custom-checks (issue #84).

Each case pairs a regression the previous text-presence check waved through
with a valid shape the new check must keep allowing, so a future edit that
reintroduces the blind spot fails here before CI has to catch it. The check
logic lives in .github/scripts/custom_checks.py; these tests need no host
library and run in every CI test job.
"""

import importlib.util
import json
import subprocess
import textwrap
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
_SCRIPT = PLUGIN_DIR / ".github" / "scripts" / "custom_checks.py"
_spec = importlib.util.spec_from_file_location("custom_checks", _SCRIPT)
custom_checks = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(custom_checks)


# ── blocking-handlers-use-def ────────────────────────────────────────────────

def test_blocking_handler_flags_direct_blocking_call():
    src = textwrap.dedent(
        """
        import time

        async def handler():
            time.sleep(1)
        """
    )
    errors = custom_checks.check_blocking_handlers(src)
    assert len(errors) == 1
    assert "handler" in errors[0]


def test_blocking_handler_flags_blocking_call_despite_executor_hint():
    """The regression the old check missed: a run_in_executor/to_thread hint
    anywhere in the coroutine exempted every blocking call in it, so an
    async handler that awaited an executor *and* slept on the loop passed."""
    src = textwrap.dedent(
        """
        import asyncio, time

        async def handler():
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, parse)
            time.sleep(1)
        """
    )
    assert custom_checks.check_blocking_handlers(src)


def test_blocking_handler_flags_inline_helper_not_handed_to_executor():
    src = textwrap.dedent(
        """
        import asyncio, time

        async def handler():
            def _helper():
                time.sleep(1)
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, _worker)
            return _helper()
        """
    )
    assert custom_checks.check_blocking_handlers(src)


def test_blocking_handler_allows_executor_worker():
    src = textwrap.dedent(
        """
        import asyncio, time

        async def handler():
            def _worker():
                time.sleep(1)
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, _worker)
        """
    )
    assert custom_checks.check_blocking_handlers(src) == []


def test_blocking_handler_allows_blocking_call_inside_to_thread_args():
    src = textwrap.dedent(
        """
        import asyncio

        async def handler(path):
            return await asyncio.to_thread(open, path)
        """
    )
    assert custom_checks.check_blocking_handlers(src) == []


def test_blocking_handler_flags_blocking_call_evaluated_in_executor_args():
    """`to_thread(open(path))` calls open() on the loop and only hands the
    already-open file to the thread — the callable form is deferred, this
    one is not."""
    src = textwrap.dedent(
        """
        import asyncio

        async def handler(path):
            return await asyncio.to_thread(open(path))
        """
    )
    assert custom_checks.check_blocking_handlers(src)


def test_blocking_handler_allows_asyncio_sleep():
    """asyncio.sleep is the non-blocking yield, not a blocking call — the
    attribute must not be matched on name alone."""
    src = textwrap.dedent(
        """
        import asyncio

        async def handler():
            await asyncio.sleep(1)
        """
    )
    assert custom_checks.check_blocking_handlers(src) == []


def test_blocking_handler_allows_non_blocking_time_helpers():
    """time.monotonic()/time.time() are clock reads, not blocking — only
    time.sleep blocks."""
    src = textwrap.dedent(
        """
        import time

        async def handler():
            return time.monotonic()
        """
    )
    assert custom_checks.check_blocking_handlers(src) == []


def test_blocking_handler_flags_time_sleep():
    src = textwrap.dedent(
        """
        import time

        async def handler():
            time.sleep(1)
        """
    )
    errors = custom_checks.check_blocking_handlers(src)
    assert errors and "time.sleep" in errors[0]


def test_blocking_handler_allows_open_method_on_custom_receiver():
    """A .open() method on an archive/file object is not the builtin open()."""
    src = textwrap.dedent(
        """
        async def handler(archive):
            return archive.open('member')
        """
    )
    assert custom_checks.check_blocking_handlers(src) == []


def test_blocking_handler_allows_plain_def_route_handler():
    """Plain def handlers run in the host's threadpool — blocking there is
    the sanctioned escape hatch the check exists to point people at."""
    src = textwrap.dedent(
        """
        def handler():
            with open('x') as fh:
                return fh.read()
        """
    )
    assert custom_checks.check_blocking_handlers(src) == []


def test_current_routes_pass_blocking_check():
    routes = (PLUGIN_DIR / "routes.py").read_text(encoding="utf-8")
    assert custom_checks.check_blocking_handlers(routes, filename="routes.py") == []


# ── idempotent-top-level-guard ───────────────────────────────────────────────

def test_reload_guard_flags_unguarded_top_level_listener():
    js = "window.addEventListener('click', onClick);\n"
    assert custom_checks.check_reload_guard(js)


def test_reload_guard_allows_listener_under_nearby_guard():
    js = textwrap.dedent(
        """
        (function () {
        if (window.__feedpakrInstalled) return;
        window.__feedpakrInstalled = true;
        window.addEventListener('click', onClick);
        })();
        """
    )
    assert custom_checks.check_reload_guard(js) == []


def test_reload_guard_flags_distant_unrelated_guard():
    """The old check's blind spot: any window.__ reference anywhere in the
    file satisfied every registration site, so an unrelated guard flag far
    away let an unguarded listener through."""
    filler = "\n".join(f"const x{i} = {i};" for i in range(60))
    js = (
        "window.__somethingElse = true;\n"
        f"{filler}\n"
        "window.addEventListener('click', onClick);\n"
    )
    assert custom_checks.check_reload_guard(js)


def test_reload_guard_ignores_element_level_listeners_and_timeouts():
    js = "dropzone.addEventListener('click', (e) => {});\nsetTimeout(() => {}, 100);\n"
    assert custom_checks.check_reload_guard(js) == []


def test_current_screen_js_passes_reload_guard():
    screen = (PLUGIN_DIR / "screen.js").read_text(encoding="utf-8")
    assert custom_checks.check_reload_guard(screen, filename="screen.js") == []


# ── version-bumped-on-change / changelog-updated ─────────────────────────────

@pytest.mark.parametrize(
    "path",
    [
        "routes.py",
        "screen.js",
        "screen.html",
        "assets/plugin.css",
        "assets/schemas/manifest.schema.json",
        "scripts/thing.schema.json",
    ],
)
def test_functional_paths_include_source_css_and_schemas(path):
    assert custom_checks.is_functional_path(path)


@pytest.mark.parametrize(
    "path",
    [
        "tests/test_x.py",
        "test/test_x.py",
        ".github/workflows/custom-checks.yml",
        ".github/scripts/custom_checks.py",
        "README.md",
        "CHANGELOG.md",
        "plugin.json",
        "assets/fixtures/song.json",
    ],
)
def test_functional_paths_exempt_tests_ci_docs_and_non_schema_data(path):
    assert not custom_checks.is_functional_path(path)


VERSION_ONLY_OLD = '{"version": "0.10.0", "name": "Feedpakr", "id": "feedpakr"}'
VERSION_ONLY_NEW = '{"version": "0.10.1", "name": "Feedpakr", "id": "feedpakr"}'


def test_version_only_manifest_change_is_not_functional():
    assert not custom_checks.manifest_change_is_functional(VERSION_ONLY_OLD, VERSION_ONLY_NEW)


def test_manifest_field_change_is_functional():
    new = '{"version": "0.10.1", "name": "Feedpakr", "id": "feedpakr", "minHost": "0.3.0"}'
    assert custom_checks.manifest_change_is_functional(VERSION_ONLY_OLD, new)


def test_single_line_manifest_field_change_is_functional():
    """Regression: a line-based version filter (the first draft) skipped
    minified manifests entirely, because the whole file — version included —
    is a single diff line that mentions "version"."""
    old = '{"version": "1.0.0"}'
    new = '{"version": "1.0.1", "minHost": "0.3.0"}'
    assert custom_checks.manifest_change_is_functional(old, new)


def test_unparseable_manifest_change_is_functional():
    assert custom_checks.manifest_change_is_functional("{not json", "{still not json")


def test_missing_base_manifest_is_functional():
    assert custom_checks.manifest_change_is_functional("", VERSION_ONLY_NEW)


# ── git-backed policy glue ───────────────────────────────────────────────────

def _git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout


def _init_repo(path):
    _git(path, "init")
    _git(path, "config", "user.email", "ci@example.invalid")
    _git(path, "config", "user.name", "CI")
    _git(path, "config", "commit.gpgsign", "false")
    return path


def _commit_all(path, message):
    _git(path, "add", "-A")
    _git(path, "commit", "-m", message)
    return _git(path, "rev-parse", "HEAD").strip()


def test_functional_changes_flags_css_schema_and_manifest_fields(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "assets" / "schemas").mkdir(parents=True)
    (repo / "assets" / "plugin.css").write_text("body {}\n", encoding="utf-8")
    (repo / "plugin.json").write_text(
        json.dumps({"version": "1.0.0", "name": "x"}), encoding="utf-8",
    )
    base = _commit_all(repo, "base")

    (repo / "assets" / "plugin.css").write_text("body { color: red }\n", encoding="utf-8")
    (repo / "assets" / "schemas" / "manifest.schema.json").write_text("{}", encoding="utf-8")
    (repo / "plugin.json").write_text(
        json.dumps({"version": "1.0.1", "name": "x", "minHost": "0.3.0"}),
        encoding="utf-8",
    )
    _commit_all(repo, "functional changes")

    changed = custom_checks.functional_changes(base, plugin_dir=str(repo))
    assert "assets/plugin.css" in changed
    assert "assets/schemas/manifest.schema.json" in changed
    assert any(p.startswith("plugin.json") for p in changed)


def test_functional_changes_ignores_ci_only_and_version_only_changes(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / ".github" / "workflows" / "custom-checks.yml").write_text(
        "name: custom-checks\n", encoding="utf-8",
    )
    (repo / "plugin.json").write_text(json.dumps({"version": "1.0.0"}), encoding="utf-8")
    base = _commit_all(repo, "base")

    (repo / ".github" / "workflows" / "custom-checks.yml").write_text(
        "name: custom-checks-v2\n", encoding="utf-8",
    )
    (repo / "plugin.json").write_text(json.dumps({"version": "1.0.1"}), encoding="utf-8")
    _commit_all(repo, "CI change plus version-only bump")

    assert custom_checks.functional_changes(base, plugin_dir=str(repo)) == []


# ── new-modules-have-tests ───────────────────────────────────────────────────

def test_module_reference_requires_real_use_not_a_mention():
    comment_only = "# see feedpakr_newmod.py for details\n\ndef test_x():\n    assert True\n"
    assert not custom_checks.module_referenced_in(comment_only, "feedpakr_newmod")

    docstring_prose = '"""Covers feedpakr_newmod.py end to end."""\n'
    assert not custom_checks.module_referenced_in(docstring_prose, "feedpakr_newmod")

    bare_docstring = '"""feedpakr_newmod"""\n'
    assert not custom_checks.module_referenced_in(bare_docstring, "feedpakr_newmod")


@pytest.mark.parametrize(
    "source",
    [
        "import feedpakr_newmod\n",
        "from feedpakr_newmod import helper\n",
        "import feedpakr_newmod as mod\n",
        "def test_x():\n    assert load('feedpakr_newmod')\n",
    ],
)
def test_module_reference_accepts_imports_and_module_name_literals(source):
    assert custom_checks.module_referenced_in(source, "feedpakr_newmod")


def test_unreferenced_new_modules_rejects_mention_only_test(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "tests").mkdir()
    (repo / "tests" / "test_old.py").write_text("def test_old():\n    assert True\n", encoding="utf-8")
    base = _commit_all(repo, "base")

    (repo / "feedpakr_newmod.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "tests" / "test_newmod.py").write_text(
        '"""Covers feedpakr_newmod.py end to end."""\n\n\ndef test_smoke():\n    assert True\n',
        encoding="utf-8",
    )
    _commit_all(repo, "module added with a mention-only test")
    assert custom_checks.unreferenced_new_modules(base, plugin_dir=str(repo)) == [
        "feedpakr_newmod.py"
    ]

    (repo / "tests" / "test_newmod.py").write_text(
        "import feedpakr_newmod\n\n\ndef test_smoke():\n"
        "    assert feedpakr_newmod.VALUE == 1\n",
        encoding="utf-8",
    )
    _commit_all(repo, "test now imports the module")
    assert custom_checks.unreferenced_new_modules(base, plugin_dir=str(repo)) == []
