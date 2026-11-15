"""Live-tree code search for Deskhand (100.046, vault B34).

Fast, correct search across a repo (by text, filename, symbol): ripgrep
as the fast path, a pure-Python walk as the correctness reference.

Ownership: this module is READ-ONLY. It creates no files, writes nothing
into the searched tree, keeps no index and no cache — every call walks
the live tree (``os.scandir`` recursion, or ``rg`` over the live tree),
so a file that exists is found. That is the whole point of B34: the
laptop-agent files-search once missed ``REPOSITORY_AUDIT.md`` entirely
(a normal file 3+ levels deep in a ``docs/`` dir); a live walk cannot
miss it, and a "not found" always names the exact root searched.

Safety (BUILD-SPEC-100046 SS1.8):
  * No shell anywhere — every subprocess call is an argv list
    (grep-verifiable: the shell flag is never enabled in this file).
  * ``root`` must be an absolute path that exists and is a directory.
  * No network. No writes. No new port/listener/grant family.
  * ``query`` / ``pattern`` / ``symbol`` must be non-empty strings.
  * File contents are returned in the op's result dict only — this
    module emits NO vm-events at all, so no token-shaped value can
    reach an event summary (E41 scrub rule, vacuous here).
  * Symlinks are followed by both engines (ripgrep gets ``--follow``);
    a symlink loop is bounded by the wall-clock cap, not by cycle
    detection.

Engine (SS1.3): ``engine`` is ``"auto"`` | ``"ripgrep"`` | ``"python"``.
``"auto"`` uses ripgrep when ``rg`` is on PATH, else Python.
``"ripgrep"`` with no ``rg`` raises ValueError — it never silently
falls back. ``DESKHAND_SEARCH_ENGINE=python`` forces the Python engine
when ``engine`` is ``"auto"`` (the documented test override). The
``engine`` field in every result reports which engine actually ran.
Ripgrep is run with ``--no-ignore`` / ``--no-ignore-global`` because
an ignored file is still a file that exists. All skip/include/exclude
POLICY is applied in Python on the results, so both engines apply the
identical rules; ripgrep is the raw enumerator, Python is the policy.

Default root (SS1.6): env ``DESKHAND_SEARCH_ROOT`` > nearest ancestor
of this module file containing ``.git`` > nearest ancestor containing
both ``Deskhand-windows`` and ``Deskhand-android`` > the directory
containing ``assistant_service/``. The resolved root is always echoed
in ``result["root"]``. Deliberately NOT the service data dir (runtime
state, not source).

Stdlib only: fnmatch, itertools, json, os, re, shutil, subprocess,
threading, time, pathlib.
"""

import fnmatch
import itertools
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

__all__ = ["Searcher", "get_searcher"]

# ---------------------------------------------------------------------------
# Caps (BUILD-SPEC SS1.5).
# ---------------------------------------------------------------------------

DEFAULT_MAX_RESULTS = 100
MAX_RESULTS = 1000
DEFAULT_TIMEOUT_S = 30
MIN_TIMEOUT_S = 1
MAX_TIMEOUT_S = 120
MAX_MATCH_TEXT_CHARS = 2000
MAX_CONTEXT_LINES = 5
BINARY_PROBE_BYTES = 8192

# Default-skipped directories (SS1.5 rule 2): build-output / tool-metadata
# dirs, never source dirs. The list is explicit and overridable per call
# via skip_defaults=False. ``.git`` is ALWAYS skipped, even then.
DEFAULT_SKIP_DIRS = frozenset({
    ".git", "__pycache__", "node_modules", ".venv", "venv",
    "bin", "obj", "build", "dist", ".gradle", ".idea", ".vs",
    "packages",
})

# Definition kinds the symbol search knows about (SS1.4).
KNOWN_KINDS = frozenset({
    "function", "class", "method", "record", "interface", "object",
})

# C# control keywords that must never be reported as a method name.
_CS_CONTROL_KEYWORDS = frozenset({
    "if", "for", "foreach", "while", "switch", "return", "using",
    "lock", "catch", "new",
})

# How often (in files) the Python walk checks the wall-clock deadline.
_DEADLINE_CHECK_EVERY = 64

# Probe name used to decide whether an explicit include glob names a
# file inside a default-skipped dir (SS1.5 rule 2): the probe has no
# extension, so a broad "*.py" does NOT exempt a skipped dir, while
# "node_modules/**" does.
_INCLUDE_PROBE = "__probe__"


# ---------------------------------------------------------------------------
# Default-root resolution (SS1.6).
# ---------------------------------------------------------------------------

def _resolve_default_root():
    """Resolve the default search root, per the SS1.6 order."""
    env_root = os.environ.get("DESKHAND_SEARCH_ROOT")
    if env_root:
        path = Path(env_root)
        if path.is_absolute() and path.is_dir():
            return path
    here = Path(__file__).resolve()
    for depth, parent in enumerate(here.parents):
        if depth >= 10:
            break
        if (parent / ".git").exists():
            return parent
        if ((parent / "Deskhand-windows").is_dir()
                and (parent / "Deskhand-android").is_dir()):
            return parent
    # Honest fallback, echoed in every result: the directory containing
    # assistant_service/ (the publish root).
    return here.parent.parent


# ---------------------------------------------------------------------------
# Validation (SS1.8 rules 2/4, SS1.5 rules 6/7).
# ---------------------------------------------------------------------------

def _validate_root(root, default_root):
    if root is None:
        path = Path(default_root)
    else:
        if not isinstance(root, str) or not root:
            raise ValueError("root must be an absolute path that exists")
        path = Path(root)
    if not path.is_absolute():
        raise ValueError("root must be an absolute path: %r" % (root,))
    if not path.is_dir():
        raise ValueError("root does not exist or is not a directory: %r"
                         % (str(path),))
    return path


def _validate_max_results(value):
    if value is None:
        return DEFAULT_MAX_RESULTS
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("max_results must be an int in 1..%d" % MAX_RESULTS)
    if value < 1:
        raise ValueError("max_results must be an int in 1..%d" % MAX_RESULTS)
    # Above the cap: clamp, do not reject (SS1.5 rule 6).
    return min(value, MAX_RESULTS)


def _validate_timeout(value):
    if value is None:
        return DEFAULT_TIMEOUT_S
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("timeout_s must be an int in %d..%d"
                         % (MIN_TIMEOUT_S, MAX_TIMEOUT_S))
    if not MIN_TIMEOUT_S <= value <= MAX_TIMEOUT_S:
        raise ValueError("timeout_s must be in %d..%d"
                         % (MIN_TIMEOUT_S, MAX_TIMEOUT_S))
    return value


def _validate_bool(value, name, default=False):
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError("%s must be a bool" % name)
    return value


def _validate_glob_list(value, name):
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValueError("%s must be a list of strings" % name)
    for item in value:
        if not isinstance(item, str) or not item:
            raise ValueError("%s must be a list of strings" % name)
    return list(value)


def _require_non_empty_string(value, name):
    if not isinstance(value, str) or not value:
        raise ValueError("%s is required and must be a non-empty string"
                         % name)
    return value


def _validate_context_lines(value):
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("context_lines must be an int in 0..%d"
                         % MAX_CONTEXT_LINES)
    if not 0 <= value <= MAX_CONTEXT_LINES:
        raise ValueError("context_lines must be in 0..%d" % MAX_CONTEXT_LINES)
    return value


def _validate_kinds(value):
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValueError("kinds must be a list of kind strings")
    for kind in value:
        if kind not in KNOWN_KINDS:
            raise ValueError("unknown kind: %r (known: %s)"
                             % (kind, sorted(KNOWN_KINDS)))
    return list(value)


def _resolve_engine(engine):
    """Resolve the engine arg to "ripgrep" or "python" (SS1.3)."""
    if engine is None:
        engine = "auto"
    if engine not in ("auto", "ripgrep", "python"):
        raise ValueError('engine must be "auto", "ripgrep" or "python"')
    if engine == "auto" and os.environ.get("DESKHAND_SEARCH_ENGINE") \
            == "python":
        return "python"
    if engine == "auto":
        return "ripgrep" if shutil.which("rg") else "python"
    if engine == "ripgrep" and not shutil.which("rg"):
        raise ValueError("ripgrep requested but not found on PATH")
    return engine

# ---------------------------------------------------------------------------
# Skip / include / exclude policy (SS1.5 rules 2/3).
# Applied in Python on every engine's output, so both engines agree.
# ---------------------------------------------------------------------------

def _glob_matches_any(rel_posix, patterns):
    """True if rel_posix matches any glob (against the full relative
    path or the basename). A ``**/`` also matches zero directories
    (globset semantics, as ripgrep's ``-g`` does): ``**/*.py``
    matches a root-level ``.py`` file, and ``src/**/*.py`` matches
    ``src/a.py`` (100.046 round 1, B3 second facet -- the old rg leg
    matched the root file under ``**/*.py`` and the Python leg did
    not, so parity needs the collapsed variant too)."""
    if not patterns:
        return False
    name = rel_posix.rsplit("/", 1)[-1]
    for pattern in patterns:
        if fnmatch.fnmatch(rel_posix, pattern):
            return True
        if fnmatch.fnmatch(name, pattern):
            return True
        if "**/" in pattern:
            collapsed = pattern.replace("**/", "")
            if fnmatch.fnmatch(rel_posix, collapsed):
                return True
            if fnmatch.fnmatch(name, collapsed):
                return True
    return False


def _include_exempts_skip_dir(rel_dir_posix, include):
    """Does an explicit include glob name a file inside this dir?

    A broad "*.py" does NOT exempt a skipped dir (the probe has no
    extension, so it cannot match); "node_modules/**" does.
    """
    if not include:
        return False
    probe = rel_dir_posix + "/" + _INCLUDE_PROBE
    for pattern in include:
        if fnmatch.fnmatch(probe, pattern):
            return True
        if fnmatch.fnmatch(rel_dir_posix, pattern):
            return True
    return False


def _dir_is_skipped(name, rel_posix, include, exclude, skip_defaults):
    """Should the walker prune this directory? ``.git`` always."""
    if name == ".git":
        return True
    if skip_defaults:
        if name in DEFAULT_SKIP_DIRS \
                and not _include_exempts_skip_dir(rel_posix, include):
            return True
        if name.startswith("."):
            return True
    return _glob_matches_any(rel_posix, exclude)


def _file_is_skipped(name, rel_posix, include, exclude, skip_defaults):
    """Should the walker skip this file (before the binary probe)?"""
    if skip_defaults and name.startswith("."):
        return True
    if include is not None and not _glob_matches_any(rel_posix, include):
        return True
    return _glob_matches_any(rel_posix, exclude)


class _BinaryFile(Exception):
    """The file's first bytes contain a NUL — skip it, never abort."""


class _UnreadableFile(Exception):
    """The file could not be opened — skip it, never abort."""


def _iter_file_lines(path):
    """Yield (lineno, text) for a text file, chunked (memory-bounded).

    Raises _BinaryFile if the first 8192 bytes contain a NUL byte,
    _UnreadableFile if the file cannot be opened. Lines are decoded
    UTF-8 with errors="replace", trailing CR stripped.
    """
    try:
        handle = open(path, "rb")
    except OSError:
        raise _UnreadableFile()
    with handle:
        head = handle.read(BINARY_PROBE_BYTES)
        if b"\x00" in head:
            raise _BinaryFile()
        pending = b""
        lineno = 0
        chunk = head
        while True:
            data = pending + chunk
            parts = data.split(b"\n")
            pending = parts.pop()
            for raw in parts:
                lineno += 1
                yield lineno, raw.decode("utf-8",
                                         errors="replace").rstrip("\r")
            chunk = handle.read(65536)
            if not chunk:
                break
        if pending:
            lineno += 1
            yield lineno, pending.decode("utf-8",
                                         errors="replace").rstrip("\r")


def _iter_candidate_files(root, include, exclude, skip_defaults,
                          deadline):
    """Yield relative Path objects for files passing the skip policy.

    Live os.scandir recursion (SS1.5 rule 1). Directories are pruned
    per _dir_is_skipped; files per _file_is_skipped. Unreadable
    directories are skipped, never aborting the walk. Raises
    TimeoutError when the deadline passes (caller turns it into a
    partial result with truncated=True).
    """
    checked = [0]
    stack = [(root, "")]
    while stack:
        directory, rel_dir = stack.pop()
        try:
            with os.scandir(directory) as entries:
                entries = list(entries)
        except OSError:
            continue
        for entry in entries:
            checked[0] += 1
            if checked[0] % _DEADLINE_CHECK_EVERY == 0:
                if time.monotonic() > deadline:
                    raise TimeoutError()
            try:
                is_dir = entry.is_dir(follow_symlinks=True)
            except OSError:
                continue
            rel = (rel_dir + "/" + entry.name) if rel_dir else entry.name
            if is_dir:
                if _dir_is_skipped(entry.name, rel, include, exclude,
                                   skip_defaults):
                    continue
                stack.append((Path(entry.path), rel))
            else:
                if _file_is_skipped(entry.name, rel, include, exclude,
                                    skip_defaults):
                    continue
                yield Path(rel)


# ---------------------------------------------------------------------------
# ripgrep helpers (SS1.3). argv lists only; output streamed through a
# reader thread so a timeout returns partial results, not nothing.
# ---------------------------------------------------------------------------

def _rg_base_args(skip_defaults):
    args = ["rg", "--no-ignore", "--no-ignore-global", "--follow"]
    if not skip_defaults:
        args.append("--hidden")
    return args


def _run_rg_stream(argv, deadline, line_cap=None, cwd=None, emit=None):
    """Run rg, streaming stdout lines to a list. Returns
    (lines, timed_out, cut_short). On timeout the child is killed and
    the lines collected so far are returned (partial results, SS1.5
    rule 7). ``line_cap`` bounds how many raw lines are collected at
    all: when hit, the child is killed and ``cut_short`` is True — this
    keeps a listing flood from growing the list without bound.
    ``emit``, when given, is called with each raw stdout line as it
    arrives (on the reader thread) instead of appending it to
    ``lines``: the ``rg --json`` caller uses it to parse entries
    streaming and keep only a bounded top-K, so a match flood never
    grows the list at all (100.046 round 1, B1 second facet).
    ``cwd`` is the directory rg runs in: ripgrep matches ``-g``
    globs relative to its cwd, so callers always pass the search root
    as cwd (the search path arg stays absolute, so output paths stay
    absolute).
    """
    lines = []
    cut_short = False
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, cwd=cwd)
    except (OSError, ValueError):
        return [], False, False

    def _reader():
        nonlocal cut_short
        try:
            for raw in proc.stdout:
                if emit is not None:
                    emit(raw)
                    continue
                lines.append(raw)
                if line_cap is not None and len(lines) >= line_cap:
                    cut_short = True
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    break
        except Exception:
            pass

    thread = threading.Thread(target=_reader, daemon=True)
    thread.start()
    timed_out = False
    while thread.is_alive():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            try:
                proc.kill()
            except Exception:
                pass
            break
        thread.join(timeout=min(remaining, 0.2))
    thread.join(timeout=2.0)
    try:
        proc.wait(timeout=2.0)
    except Exception:
        pass
    return lines, timed_out, cut_short


def _rg_json_matches(argv, deadline, match_cap, cwd, accept=None):
    """Parse an rg --json stream into (match dicts, timed_out,
    cut_short).

    The stream is capped by MATCH entries (``match_cap``), not raw
    stdout lines (100.046 round 1, B1): one matched file emits
    ``begin`` / ``match`` / ``end`` lines (plus ``context`` lines
    and a ``summary``), so a raw-line cap under-delivers — with
    ``max_results=100, context_lines=0`` the old line cap admitted
    only 67 matches.

    The cap is a bounded top-K, not a first-K (100.046 round 1,
    second facet): rg's output order is parallel/nondeterministic,
    so keeping the first ``match_cap`` matches returns a different
    set from the Python leg's first ``match_cap`` in walk order.
    Entries are parsed streaming (``emit``) and only the smallest
    ``match_cap`` matches by (path, line) are kept, so the result
    set is the deterministic top-K on both legs and a match flood
    never grows the list. The stream therefore runs to completion
    (or the deadline) instead of being killed at the cap; rg is
    fast (a 6000-file tree takes ~0.15 s) and the deadline bounds
    the pathological case.

    ``accept`` maps an absolute match path to its relative posix
    path, or None to drop it: the policy filter (include/exclude/
    skip) runs HERE, per entry, because a bounded keep taken
    before filtering could fill up on dropped matches and
    short-change the result. Dropped matches get no context
    lines attached. ``accept=None`` keeps everything.

    Matches are {"path": rel-posix, "line": int, "text": str}; context
    lines (--after-context) are appended to the preceding match's text.
    """
    keep = []
    current = [None]

    def _emit(raw):
        try:
            entry = json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError:
            return
        if not isinstance(entry, dict):
            return
        entry_type = entry.get("type")
        data = entry.get("data") or {}
        if entry_type == "match":
            path_text = (data.get("path") or {}).get("text", "")
            if accept is not None:
                rel_posix = accept(path_text)
                if rel_posix is None:
                    current[0] = None
                    return
            else:
                rel_posix = path_text
            lines_text = (data.get("lines") or {}).get("text", "")
            match = {
                "path": rel_posix,
                "line": data.get("line_number", 0),
                "text": lines_text.rstrip("\r\n"),
            }
            current[0] = match
            keep.append(match)
            if len(keep) > match_cap:
                worst = max(keep,
                            key=lambda m: (m["path"], m["line"]))
                keep.remove(worst)
        elif entry_type == "context" and current[0] is not None:
            lines_text = (data.get("lines") or {}).get("text", "")
            current[0]["text"] += "\n" + lines_text.rstrip("\r\n")

    _lines, timed_out, cut_short = _run_rg_stream(argv, deadline,
                                                  cwd=cwd, emit=_emit)
    return keep, timed_out, cut_short


def _rg_list_files(root, skip_defaults, deadline):
    """Files rg --files enumerates under root (raw enumeration; the
    caller's policy filter runs in Python on the result). Returns
    (files, timed_out) — a timed-out enumeration is partial, and the
    caller must set truncated.

    No include/exclude globs are passed to rg (100.046 round 1, B3):
    ripgrep's ``-g`` glob ``*`` does not cross ``/`` while Python's
    ``fnmatch`` ``*`` does, so ``include=['src/*.py']`` found nested
    files on the Python leg and missed them on the rg leg. Globs are
    applied purely in Python by the caller, exactly as ``exclude``
    already was — parity by construction, at the cost of rg scanning
    files the policy later drops (which it already did for
    ``exclude``).
    """
    argv = _rg_base_args(skip_defaults) + ["--files"] \
        + ["--", str(root)]
    lines, timed_out, _cut = _run_rg_stream(argv, deadline, cwd=str(root))
    files = []
    for raw in lines:
        text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        if text:
            files.append(text)
    return files, timed_out


def _rel_passes_policy(rel_posix, include, exclude, skip_defaults):
    """Does this relative path pass the skip/include/exclude policy
    (SS1.5 rules 2/3)? Checks every ancestor directory as well as the
    file itself — the same rule the Python walker applies inline."""
    parts = rel_posix.split("/")
    for depth in range(1, len(parts)):
        dir_rel = "/".join(parts[:depth])
        if _dir_is_skipped(parts[depth - 1], dir_rel, include, exclude,
                           skip_defaults):
            return False
    return not _file_is_skipped(parts[-1], rel_posix, include, exclude,
                                skip_defaults)


def _count_text_files(root, rel_paths):
    """How many of these relative paths are readable text files
    (SS1.5 rule 8, text-search scanned_files)? Binary-probed-out and
    unreadable files are not counted — the same rule the Python leg
    applies when it opens each file."""
    count = 0
    for rel in rel_paths:
        try:
            with open(root / rel, "rb") as handle:
                if b"\x00" in handle.read(BINARY_PROBE_BYTES):
                    continue
        except OSError:
            continue
        count += 1
    return count


def _scan_symbol_file(path, rel_posix, suffix, symbol, kinds):
    """Scan one file for definitions of ``symbol``.

    Returns (opened, definitions) where opened is False when the file
    was binary or unreadable (SS1.5 rules 4/5), and definitions are
    {"path" (relative), "line", "kind", "signature"} dicts.
    """
    try:
        lines = _iter_file_lines(path)
        first = next(lines)
    except (_BinaryFile, _UnreadableFile):
        return False, []
    except StopIteration:
        return True, []
    definitions = []
    for lineno, text in itertools.chain([first], lines):
        found = _symbol_for_line(suffix, text)
        if found is None:
            continue
        kind, name = found
        if name != symbol:
            continue
        if kinds is not None and kind not in kinds:
            continue
        definitions.append({
            "path": rel_posix,
            "line": lineno,
            "kind": kind,
            "signature": text.strip()[:MAX_MATCH_TEXT_CHARS],
        })
    return True, definitions

# ---------------------------------------------------------------------------
# Symbol definitions (SS1.4). Each entry: (kind, compiled regex) where
# group 1 is the definition name. Checked in order per file.
# ---------------------------------------------------------------------------

def _compile_patterns():
    patterns = {
        ".py": [
            ("function", re.compile(
                r"^\s*(?:async\s+)?def\s+(\w+)\s*\(")),
            ("class", re.compile(
                r"^\s*class\s+(\w+)\s*[\(:]")),
        ],
        ".cs": [
            ("class", re.compile(
                r"^\s*(?:(?:public|private|protected|internal|sealed|abstract|static|partial)\s+)*class\s+(\w+)")),
            ("record", re.compile(
                r"^\s*(?:(?:public|private|protected|internal|sealed|abstract|static|partial)\s+)*record\s+(\w+)")),
            ("interface", re.compile(
                r"^\s*(?:(?:public|private|protected|internal|sealed|abstract|static|partial)\s+)*interface\s+(\w+)")),
            # Method: return-type + name + "(". Group 1 is the return
            # type, group 2 the name; control keywords and calls
            # (line ends in ";") are filtered in _symbol_for_line.
            ("method", re.compile(
                r"^\s*(?:(?:public|private|protected|internal|static|async|virtual|override|sealed|new|extern|unsafe|partial)\s+)*([\w<>\[\]\.,\?]+)\s+(\w+)\s*\(")),
        ],
        ".kt": [
            ("function", re.compile(
                r"^\s*(?:(?:public|private|protected|internal|open|abstract|override|suspend|inline|tailrec|operator|infix)\s+)*fun\s+(?:<[^>]+>\s+)?(?:[\w<>]+\.)?(\w+)\s*\(")),
            ("class", re.compile(
                r"^\s*(?:(?:data|sealed|open|abstract)\s+)*class\s+(\w+)")),
            ("interface", re.compile(r"^\s*interface\s+(\w+)")),
            ("object", re.compile(r"^\s*object\s+(\w+)")),
        ],
        ".js": [
            ("function", re.compile(
                r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+(\w+)\s*\(")),
            ("class", re.compile(
                r"^\s*(?:export\s+)?(?:default\s+)?class\s+(\w+)")),
        ],
        ".ts": [
            ("function", re.compile(
                r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+(\w+)\s*\(")),
            ("class", re.compile(
                r"^\s*(?:export\s+)?(?:default\s+)?class\s+(\w+)")),
        ],
        ".jsx": [
            ("function", re.compile(
                r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+(\w+)\s*\(")),
            ("class", re.compile(
                r"^\s*(?:export\s+)?(?:default\s+)?class\s+(\w+)")),
        ],
        ".tsx": [
            ("function", re.compile(
                r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+(\w+)\s*\(")),
            ("class", re.compile(
                r"^\s*(?:export\s+)?(?:default\s+)?class\s+(\w+)")),
        ],
        ".ps1": [
            ("function", re.compile(r"^\s*function\s+([\w-]+)")),
        ],
        ".go": [
            ("function", re.compile(
                r"^\s*func\s+(?:\(\s*\w+\s+\*?\w+\s*\)\s*)?(\w+)\s*\(")),
        ],
    }
    return patterns


_SYMBOL_PATTERNS = _compile_patterns()


def _symbol_for_line(suffix, line):
    """(kind, name) if this line defines a symbol of a known kind for
    the file's extension, else None. The C# method pattern needs its
    extra filters (control keywords, calls ending in ";")."""
    for kind, regex in _SYMBOL_PATTERNS.get(suffix, ()):  # noqa: B905
        match = regex.match(line)
        if not match:
            continue
        if kind == "method" and suffix == ".cs":
            name = match.group(2)
            if name in _CS_CONTROL_KEYWORDS:
                continue
            if line.rstrip().endswith(";"):
                continue
            return kind, name
        return kind, match.group(1)
    return None


# ---------------------------------------------------------------------------
# Searcher
# ---------------------------------------------------------------------------

class Searcher:
    """Live-tree search. No state between calls except the resolved
    default root — no index, no cache, no listing kept anywhere.

    Constructed as ``Searcher(default_root=None)``; the service wires
    ``get_searcher(service)`` (below).
    """

    def __init__(self, default_root=None):
        if default_root is None:
            default_root = _resolve_default_root()
        self._default_root = Path(default_root)

    # ------------------------------------------------------------------
    # Public ops (SS1.1 shapes)
    # ------------------------------------------------------------------

    def search_text(self, query, root=None, include=None, exclude=None,
                    max_results=DEFAULT_MAX_RESULTS, case_sensitive=False,
                    context_lines=0, engine="auto", timeout_s=None,
                    skip_defaults=True):
        """Literal-substring search over live text files."""
        query = _require_non_empty_string(query, "query")
        root_path = _validate_root(root, self._default_root)
        include = _validate_glob_list(include, "include")
        exclude = _validate_glob_list(exclude, "exclude")
        max_results = _validate_max_results(max_results)
        case_sensitive = _validate_bool(case_sensitive, "case_sensitive")
        context_lines = _validate_context_lines(context_lines)
        timeout_s = _validate_timeout(timeout_s)
        skip_defaults = _validate_bool(skip_defaults, "skip_defaults",
                                       default=True)
        engine = _resolve_engine(engine)

        started = time.monotonic()
        deadline = started + timeout_s
        root_str = str(root_path)
        if engine == "ripgrep":
            matches, timed_out, scanned = self._text_via_rg(
                query, root_path, include, exclude, max_results,
                case_sensitive, context_lines, skip_defaults, deadline)
        else:
            matches, timed_out, scanned = self._text_via_python(
                query, root_path, include, exclude, max_results,
                case_sensitive, context_lines, skip_defaults, deadline)
        truncated = timed_out or len(matches) > max_results
        # Deterministic order: sort BEFORE slicing, or each engine
        # keeps the first max_results in its own collection order
        # and the capped sets disagree (100.046 round 1: both legs
        # returned 100 matches but different ones -- rg output order
        # is parallel/nondeterministic, the Python walk is DFS).
        matches.sort(key=lambda m: (m["path"], m["line"]))
        matches = matches[:max_results]
        return {
            "ok": True,
            "query": query,
            "root": root_str,
            "engine": engine,
            "matches": matches,
            "truncated": bool(truncated),
            "scanned_files": scanned,
            "duration_ms": int((time.monotonic() - started) * 1000),
        }

    def search_files(self, pattern, root=None,
                     max_results=DEFAULT_MAX_RESULTS, engine="auto",
                     timeout_s=None, skip_defaults=True):
        """Live filename search (glob or substring). Never an index."""
        pattern = _require_non_empty_string(pattern, "pattern")
        root_path = _validate_root(root, self._default_root)
        max_results = _validate_max_results(max_results)
        timeout_s = _validate_timeout(timeout_s)
        skip_defaults = _validate_bool(skip_defaults, "skip_defaults",
                                       default=True)
        engine = _resolve_engine(engine)

        started = time.monotonic()
        deadline = started + timeout_s
        root_str = str(root_path)
        if engine == "ripgrep":
            files, scanned, timed_out = self._files_via_rg(
                pattern, root_path, max_results, skip_defaults, deadline)
        else:
            files, scanned, timed_out = self._files_via_python(
                pattern, root_path, max_results, skip_defaults, deadline)
        truncated = timed_out or len(files) > max_results
        return {
            "ok": True,
            "pattern": pattern,
            "root": root_str,
            "engine": engine,
            "files": sorted(files)[:max_results],
            "truncated": bool(truncated),
            "scanned_files": scanned,
            "duration_ms": int((time.monotonic() - started) * 1000),
        }

    def search_symbol(self, symbol, root=None, kinds=None,
                      max_results=DEFAULT_MAX_RESULTS, engine="auto",
                      timeout_s=None, skip_defaults=True):
        """Definition search by exact (case-sensitive) symbol name."""
        symbol = _require_non_empty_string(symbol, "symbol")
        root_path = _validate_root(root, self._default_root)
        kinds = _validate_kinds(kinds)
        max_results = _validate_max_results(max_results)
        timeout_s = _validate_timeout(timeout_s)
        skip_defaults = _validate_bool(skip_defaults, "skip_defaults",
                                       default=True)
        engine = _resolve_engine(engine)

        started = time.monotonic()
        deadline = started + timeout_s
        root_str = str(root_path)
        # Both engines read candidate files with the same definition
        # patterns (SS1.4); ripgrep is the file enumerator on its leg,
        # so the regexes — and therefore the definitions found — are
        # identical by construction. scanned_files is the number of
        # pattern-suffix files actually opened (binary / unreadable
        # excluded), counted the same way on both legs (SS1.5 rule 8):
        # the walk completes even after the definition cap is hit, so
        # a capped call reports the same full count on both legs
        # (100.046 round 1, B2, same class as the text leg).
        timed_out = False
        definitions = []
        scanned = 0
        try:
            if engine == "ripgrep":
                candidates, list_timed_out = self._candidates_via_rg(
                    root_path, skip_defaults, deadline)
                timed_out = timed_out or list_timed_out
                iterator = iter(candidates)
            else:
                iterator = _iter_candidate_files(
                    root_path, None, None, skip_defaults, deadline)
            for rel in iterator:
                if time.monotonic() > deadline:
                    timed_out = True
                    break
                path = root_path / rel
                suffix = path.suffix.lower()
                if suffix not in _SYMBOL_PATTERNS:
                    continue
                opened, found = _scan_symbol_file(
                    path, rel.as_posix(), suffix, symbol, kinds)
                if not opened:
                    continue
                scanned += 1
                # Bounded top-K by (path, line), not first-K (100.046
                # round 1, second facet): the rg leg iterates sorted
                # candidates while the Python leg walks DFS, so
                # stopping at the cap would keep different subsets.
                definitions.extend(found)
                if len(definitions) > max_results:
                    definitions.sort(
                        key=lambda d: (d["path"], d["line"]))
                    definitions = definitions[:max_results + 1]
        except TimeoutError:
            timed_out = True
        truncated = timed_out or len(definitions) > max_results
        # Sort BEFORE slicing (same reason as search_text: the two
        # legs collect in different orders, so slicing first keeps
        # different capped sets).
        definitions.sort(key=lambda d: (d["path"], d["line"]))
        definitions = definitions[:max_results]
        return {
            "ok": True,
            "symbol": symbol,
            "root": root_str,
            "engine": engine,
            "definitions": definitions,
            "truncated": bool(truncated),
            "scanned_files": scanned,
            "duration_ms": int((time.monotonic() - started) * 1000),
        }

    def dispatch(self, op, command):
        """One dispatch entry point for the service handle() branch
        (SS1.2). ``command`` is the op's args dict."""
        if not isinstance(command, dict):
            raise ValueError("command must be a dict of args")
        if op == "search_text":
            return self.search_text(
                command.get("query"),
                root=command.get("root"),
                include=command.get("include"),
                exclude=command.get("exclude"),
                max_results=command.get("max_results", DEFAULT_MAX_RESULTS),
                case_sensitive=command.get("case_sensitive", False),
                context_lines=command.get("context_lines", 0),
                engine=command.get("engine", "auto"),
                timeout_s=command.get("timeout_s"),
                skip_defaults=command.get("skip_defaults", True),
            )
        if op == "search_files":
            return self.search_files(
                command.get("pattern"),
                root=command.get("root"),
                max_results=command.get("max_results", DEFAULT_MAX_RESULTS),
                engine=command.get("engine", "auto"),
                timeout_s=command.get("timeout_s"),
                skip_defaults=command.get("skip_defaults", True),
            )
        if op == "search_symbol":
            return self.search_symbol(
                command.get("symbol"),
                root=command.get("root"),
                kinds=command.get("kinds"),
                max_results=command.get("max_results", DEFAULT_MAX_RESULTS),
                engine=command.get("engine", "auto"),
                timeout_s=command.get("timeout_s"),
                skip_defaults=command.get("skip_defaults", True),
            )
        raise ValueError("unknown search op: %r" % (op,))

    # ------------------------------------------------------------------
    # Engines (SS1.3/SS1.5). Each returns (results, timed_out, scanned).
    # Policy (skip/include/exclude) is applied in Python on both legs,
    # so the two engines agree on the same tree.
    # ------------------------------------------------------------------

    def _text_via_python(self, query, root_path, include, exclude,
                         max_results, case_sensitive, context_lines,
                         skip_defaults, deadline):
        """Pure-Python text search: the correctness reference.

        ``scanned_files`` is the FULL count of readable text files
        passing the policy under the root (100.046 round 1, B2):
        the walk always completes (unless the deadline hits), so
        capped and uncapped calls agree across engines. Only a
        timeout leaves ``scanned_files`` partial (and then
        ``truncated`` is true).

        Matches are kept as a bounded top-K, not a first-K
        (100.046 round 1, second facet): only the smallest
        ``max_results + 1`` matches by (path, line) survive, so
        the capped set is the deterministic top-K on both legs --
        the walk order here and rg's parallel order would
        otherwise keep different first-K subsets.
        """
        needle = query if case_sensitive else query.lower()
        matches = []
        scanned = 0
        timed_out = False
        try:
            iterator = _iter_candidate_files(
                root_path, include, exclude, skip_defaults, deadline)
            for rel in iterator:
                if time.monotonic() > deadline:
                    timed_out = True
                    break
                path = root_path / rel
                try:
                    line_iter = _iter_file_lines(path)
                    first = next(line_iter)
                except (_BinaryFile, _UnreadableFile):
                    continue
                except StopIteration:
                    scanned += 1
                    continue
                scanned += 1
                pending_context = []
                for lineno, text in itertools.chain([first], line_iter):
                    if time.monotonic() > deadline:
                        timed_out = True
                        break
                    hay = text if case_sensitive else text.lower()
                    if needle in hay:
                        match = {
                            "path": rel.as_posix(),
                            "line": lineno,
                            "text": text[:MAX_MATCH_TEXT_CHARS],
                        }
                        matches.append(match)
                        pending_context = [match]
                        # Keep max_results + 1: the extra entry is
                        # what makes truncated exact in search_text
                        # (len > max_results iff more matches exist).
                        if len(matches) > max_results + 1:
                            worst = max(
                                matches,
                                key=lambda m: (m["path"], m["line"]))
                            matches.remove(worst)
                    elif pending_context and context_lines > 0:
                        match = pending_context[0]
                        existing = match["text"].count("\n")
                        if existing < context_lines:
                            match["text"] = (
                                match["text"] + "\n" + text
                            )[:MAX_MATCH_TEXT_CHARS]
                if timed_out:
                    break
        except TimeoutError:
            timed_out = True
        return matches, timed_out, scanned

    def _text_via_rg(self, query, root_path, include, exclude,
                     max_results, case_sensitive, context_lines,
                     skip_defaults, deadline):
        """ripgrep text search: the speed path. Policy is applied in
        Python on rg's output — ``include`` is NOT passed to rg as
        ``-g`` (100.046 round 1, B3); ``scanned_files`` comes from a
        second, always-complete ``rg --files`` enumeration, probed
        for binary (SS1.5 rule 8), so it counts the same full file
        set the Python leg opens (100.046 round 1, B2)."""
        argv = _rg_base_args(skip_defaults) + [
            "--json", "--line-number", "--fixed-strings",
        ]
        if not case_sensitive:
            argv.append("--ignore-case")
        if context_lines > 0:
            argv += ["--after-context", str(context_lines)]
        argv += ["--", query, str(root_path)]
        # Cap the MATCHES collected: count "match" entries in the
        # --json stream, not raw stdout lines (100.046 round 1,
        # B1). One past the cap, so truncated is exact. The keep is
        # a bounded top-K by (path, line) (see _rg_json_matches),
        # and the policy filter runs per entry inside the stream.
        def _accept(absolute):
            try:
                rel = Path(absolute).relative_to(root_path)
            except ValueError:
                try:
                    rel = Path(os.path.relpath(absolute,
                                               str(root_path)))
                except ValueError:
                    return None
            rel_posix = rel.as_posix()
            if not _rel_passes_policy(rel_posix, include, exclude,
                                      skip_defaults):
                return None
            return rel_posix

        match_cap = max_results + 1
        raw_matches, timed_out, cut_short = _rg_json_matches(
            argv, deadline, match_cap, cwd=str(root_path), accept=_accept)
        # Already relative, policy-filtered, and bounded by the
        # streaming keep; only the per-match text cap is left here.
        matches = [{
            "path": match["path"],
            "line": match["line"],
            "text": match["text"][:MAX_MATCH_TEXT_CHARS],
        } for match in raw_matches]
        # scanned_files: files rg would list, policy-filtered, that
        # are readable text (binary-probed-out excluded). The
        # enumeration is always complete, never stopped at the match
        # cap (100.046 round 1, B2).
        enumerated, list_timed_out = _rg_list_files(
            root_path, skip_defaults, deadline)
        rels = []
        for absolute in enumerated:
            try:
                rel = Path(absolute).relative_to(root_path)
            except ValueError:
                continue
            rel_posix = rel.as_posix()
            if _rel_passes_policy(rel_posix, include, exclude,
                                  skip_defaults):
                rels.append(Path(rel_posix))
        scanned = _count_text_files(root_path, rels)
        truncated = (timed_out or cut_short or list_timed_out
                     or len(matches) > max_results)
        return matches, truncated, scanned

    def _files_via_python(self, pattern, root_path, max_results,
                          skip_defaults, deadline):
        """Live os.scandir filename search (SS1.1: never an index).

        Files are kept as a bounded top-K by name (100.046 round 1,
        second facet), and the walk completes: the enumeration order
        here and rg's --files order differ, so stopping at the cap
        would keep different subsets. ``scanned_files`` is therefore
        the full count on both legs, as for the text leg."""
        if any(ch in pattern for ch in "*?["):
            is_glob = True
        else:
            is_glob = False
        files = []
        scanned = 0
        timed_out = False
        try:
            iterator = _iter_candidate_files(
                root_path, None, None, skip_defaults, deadline)
            for rel in iterator:
                if time.monotonic() > deadline:
                    timed_out = True
                    break
                scanned += 1
                rel_posix = rel.as_posix()
                if is_glob:
                    if _glob_matches_any(rel_posix, [pattern]):
                        files.append(rel_posix)
                elif pattern in rel_posix:
                    files.append(rel_posix)
                # Keep max_results + 1: the extra entry is what
                # makes truncated exact in search_files.
                if len(files) > max_results + 1:
                    files.remove(max(files))
        except TimeoutError:
            timed_out = True
        return files, scanned, timed_out

    def _files_via_rg(self, pattern, root_path, max_results,
                      skip_defaults, deadline):
        """``rg --files`` over the live tree, policy-filtered in
        Python; glob and substring patterns are both filtered in
        Python after the listing, so ``scanned_files`` counts the
        same file set the Python leg scans (SS1.5 rule 8). Files are
        kept as a bounded top-K by name (100.046 round 1, second
        facet) over the complete enumeration, not the first
        ``max_results`` in rg's listing order."""
        enumerated, list_timed_out = _rg_list_files(
            root_path, skip_defaults, deadline)
        files = []
        scanned = 0
        timed_out = list_timed_out
        for absolute in enumerated:
            if time.monotonic() > deadline:
                timed_out = True
                break
            try:
                rel = Path(absolute).relative_to(root_path)
            except ValueError:
                continue
            rel_posix = rel.as_posix()
            if not _rel_passes_policy(rel_posix, None, None,
                                      skip_defaults):
                continue
            scanned += 1
            if _glob_matches_any(rel_posix, [pattern]) \
                    or pattern in rel_posix:
                files.append(rel_posix)
            # Keep max_results + 1: the extra entry is what makes
            # truncated exact in search_files.
            if len(files) > max_results + 1:
                files.remove(max(files))
        return files, scanned, timed_out

    def _candidates_via_rg(self, root_path, skip_defaults, deadline):
        """Relative candidate files for the symbol search's ripgrep
        leg: rg --files enumeration, policy-filtered in Python.
        Returns (candidates, timed_out)."""
        enumerated, timed_out = _rg_list_files(root_path,
                                               skip_defaults, deadline)
        candidates = []
        for absolute in enumerated:
            try:
                rel = Path(absolute).relative_to(root_path)
            except ValueError:
                continue
            rel_posix = rel.as_posix()
            if _rel_passes_policy(rel_posix, None, None, skip_defaults):
                candidates.append(Path(rel_posix))
        candidates.sort(key=lambda p: p.as_posix())
        return candidates, timed_out


# ---------------------------------------------------------------------------
# Service accessor (SS1.2): one lazily-created Searcher per service.
# ---------------------------------------------------------------------------

def get_searcher(service):
    """Return the service's single Searcher, creating it on first use.

    The Searcher needs no event sink and no data dir; its default root
    is resolved from the module file (SS1.6). The service branch does
    a lazy ``from deskhand_search import get_searcher`` so a broken
    search module cannot break service startup.
    """
    searcher = getattr(service, "_searcher", None)
    if searcher is None:
        searcher = Searcher()
        service._searcher = searcher
    return searcher
