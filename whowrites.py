#!/usr/bin/env python
"""Name every call site that empties, drops or rewrites a feature class, and never name a reader as the thing destroyed.

A published layer comes up empty on a Monday morning. No geodatabase records
who wrote to it, and a scheduled task records a command line and nothing else,
so the only lineage anybody has is the script share. The admin greps it for the
feature class name and gets five files back. Four of them only read the layer.
The fifth is the one that truncated it, and the truncate line does not contain
the name at all, because the path was assembled with os.path.join three hundred
lines earlier.

grep is the tool everybody reaches for here and it answers a different
question: it finds the files that mention a string. It cannot tell a read from
a truncate, it cannot follow a path through a variable, and it returns nothing
at all for the call site that matters most. The author's own 37 repositories do
not close this gap either. safe_republish refuses one bad truncate at the
moment it runs; it does not find the others. pytlint parses scripts with ast,
but all 24 of its rules judge one file's own shape, and none of them asks which
dataset a call acts on. whobreaks
answers which portal item references another item, svcsource answers which
service reads which database, and gdbxray prints a geodatabase's own schema,
which records nothing about the scripts outside it. This tool reads Python
source, resolves the paths those scripts build, and classifies each arcpy call
site as READ, WRITE, EMPTY or DROP.

It reads source code. It never imports arcpy, never opens a geodatabase and
never changes a file, so there is nothing here to put behind --apply.

    python whowrites.py --self-test
    python whowrites.py scripts/
    python whowrites.py scripts/ --name Hydrants
    python whowrites.py scripts/ --name Hydrants --destructive-only

Exit codes: 0 nothing destructive was reported, 1 a destructive call site was
reported, 2 a file could not be parsed or read so the answer is incomplete,
64 usage error.
"""

from __future__ import print_function

import argparse
import ast
import io
import os
import re
import sys
import textwrap
import warnings

# =============================================================================
# CONFIGURATION. Deliberately not flags. Change here, not at the call site.
# =============================================================================

# Extensions that are read as Python source. .pyt is an ArcGIS Python toolbox,
# which is a plain Python module with a fixed class shape. .txt is included
# because script shares are full of .py files saved as .txt to get them past a
# mail filter, and those still run when somebody renames them back.
SOURCE_SUFFIXES = (".py", ".pyt", ".txt")

# Directories never walked. .git holds compressed objects, not source, and a
# virtual environment holds thousands of files nobody in this tree wrote.
SKIP_DIRS = (".git", "__pycache__", ".venv", "venv", ".idea", ".vscode")

# Nothing larger than this is parsed. A 40 MB "script" is generated data, and
# ast.parse on it costs minutes for an answer nobody wants.
MAX_SOURCE_BYTES = 4 * 1000 * 1000

# How many times the file is re-read to carry a path through a call. Each pass
# binds one more hop, so 3 resolves main() -> process() -> replace(). A chain
# longer than this stays unresolved, and unresolved is reported, never guessed.
MAX_BINDING_PASSES = 3

# =============================================================================
# End of CONFIGURATION.
# =============================================================================

# The marker for a piece of a path this tool could not resolve. It travels
# inside the resolved string, so "prefix + name" where prefix is unknown stays
# visibly unknown at the point it matters. Choosing a character that cannot
# occur in a source string literal is what makes that safe.
UNKNOWN = "\x00"

# The four verdicts. DROP removes the dataset object itself, EMPTY removes
# every row and leaves the object, WRITE adds or changes rows, READ touches
# nothing. DROP and EMPTY are what the Monday morning question is about.
DROP = "DROP"
EMPTY = "EMPTY"
WRITE = "WRITE"
READ = "READ"

# The group heading for call sites whose dataset could not be named. It is a
# sentence rather than a symbol, because this group is the tool's own blind
# spot and it should read like one.
NO_NAME = "(no resolvable name)"

SEVERITY = {DROP: 0, EMPTY: 1, WRITE: 2, READ: 3}
DESTRUCTIVE = (DROP, EMPTY)

# Each arcpy tool maps to the arguments that name a dataset, as
# (parameter name, positional index, verdict). The parameter names are Esri's,
# so the keyword form and the positional form of the same call resolve to the
# same role. A tool missing from this table is not classified at all, which is
# the safe direction: an unknown tool is never reported as a destroyer.
TOOLS = {
    "truncatetable": (("in_table", 0, EMPTY),),
    "deletefeatures": (("in_features", 0, EMPTY),),
    "deleterows": (("in_rows", 0, EMPTY),),
    "delete": (("in_data", 0, DROP),),
    "append": (("inputs", 0, READ), ("target", 1, WRITE)),
    "copyfeatures": (("in_features", 0, READ), ("out_feature_class", 1, WRITE)),
    "copyrows": (("in_rows", 0, READ), ("out_table", 1, WRITE)),
    "insertcursor": (("in_table", 0, WRITE),),
    "updatecursor": (("in_table", 0, WRITE),),
    "searchcursor": (("in_table", 0, READ),),
    "getcount": (("in_rows", 0, READ),),
    "makefeaturelayer": (("in_features", 0, READ),),
    "maketableview": (("in_table", 0, READ),),
    "exists": (("dataset", 0, READ),),
    "calculatefield": (("in_table", 0, WRITE),),
    "addfield": (("in_table", 0, WRITE),),
    "deletefield": (("in_table", 0, WRITE),),
    "selectlayerbyattribute": (("in_layer_or_view", 0, READ),),
    "featureclasstofeatureclass": (("in_features", 0, READ),),
}

# Tools that mint a layer or view name standing in for a dataset. The value is
# (parameter name, positional index) of the NAME they create. A later
# DeleteFeatures against that name empties the feature class underneath it, so
# without this the destroyer is reported against a layer nobody can find.
LAYER_MAKERS = {
    "makefeaturelayer": ("out_layer", 1),
    "maketableview": ("out_view", 1),
}

# arcpy exposes every tool twice: arcpy.TruncateTable_management and
# arcpy.management.TruncateTable are the same call.
TOOL_SUFFIXES = ("_management", "_analysis", "_conversion", "_cartography",
                 "_edit", "_da", "_server", "_stats")

# File extensions stripped before a dataset name is compared, so that
# roads.shp answers to the name roads.
DATA_SUFFIXES = (".shp", ".dbf", ".gdb", ".sde", ".lyrx", ".lyr", ".csv")

# One path separator, either spelling, one or more times. Windows accepts both
# and so do the scripts this reads.
SEP_RE = re.compile(r"[\\/]+")

# A drive letter or a leading separator. os.path.join throws away everything
# before an absolute segment, and so must this.
ABS_RE = re.compile(r"^(?:[A-Za-z]:[\\/]?|[\\/])")


# ------------------------------------------------------------------- values

def split_path(text):
    """Split a resolved path into segments on either separator."""
    parts = SEP_RE.split(text)
    # A trailing separator leaves an empty final segment, which would hide the
    # real dataset name one place to its left.
    while len(parts) > 1 and parts[-1] == "":
        parts.pop()
    return parts


def join_paths(parts):
    """Join like os.path.join, including its absolute-segment reset.

    The separator written out is always a backslash. arcpy paths are Windows
    paths, and a fixed separator is what makes this tool print the same answer
    on Windows and on Linux.
    """
    out = ""
    for part in parts:
        if out == "" or ABS_RE.match(part):
            out = part
        elif out[-1] in "\\/":
            out = out + part
        else:
            out = out + "\\" + part
    return out


def render(text):
    """The human spelling of a resolved path, with unknown pieces as ?."""
    return text.replace(UNKNOWN, "?")


def dataset(text):
    """The dataset name a path ends in, or None when that piece is unknown.

    A path whose last segment is partly unknown has no name this tool is
    willing to state. Returning a half-name here is how a reader ends up
    reported as the destroyed thing.
    """
    last = split_path(text)[-1]
    if last == "" or UNKNOWN in last:
        return None
    return last


def names(text):
    """Every name a dataset answers to, for matching a --name query.

    An enterprise geodatabase feature class is owner-qualified, so
    GIS.OWNER.Hydrants has to answer to Hydrants and to OWNER.Hydrants.
    It must NOT answer to HydrantsChanges, and HydrantsChanges must not answer
    to Hydrants, which is why every candidate here is a whole dot-separated
    segment and never a prefix.
    """
    last = dataset(text)
    if last is None:
        return ()
    out = [last]
    base = last
    for suffix in DATA_SUFFIXES:
        if base.lower().endswith(suffix):
            base = base[:-len(suffix)]
            break
    if base and base != last:
        out.append(base)
    bits = base.split(".")
    if len(bits) > 1 and bits[-1]:
        out.append(bits[-1])
    if len(bits) > 2 and bits[-2]:
        out.append(bits[-2] + "." + bits[-1])
    return tuple(out)


def matches(text, query):
    """True when a resolved path names the dataset the operator asked about."""
    wanted = query.lower()
    return any(n.lower() == wanted for n in names(text))


# ------------------------------------------------------------ the resolver

def _os_join(func):
    """True for os.path.join written the usual way."""
    return (isinstance(func, ast.Attribute) and func.attr == "join"
            and isinstance(func.value, ast.Attribute)
            and func.value.attr == "path"
            and isinstance(func.value.value, ast.Name)
            and func.value.value.id == "os")


def _os_sep(node):
    """True for os.sep and os.path.sep."""
    if not isinstance(node, ast.Attribute) or node.attr != "sep":
        return False
    base = node.value
    if isinstance(base, ast.Name):
        return base.id == "os"
    return (isinstance(base, ast.Attribute) and base.attr == "path"
            and isinstance(base.value, ast.Name) and base.value.id == "os")


def const_value(node, env):
    """Resolve an expression to a path string, marking what it cannot resolve.

    The return value is always a string. Pieces that could not be resolved are
    the UNKNOWN marker, so a partly built path keeps the part that is known.
    That matters: os.path.join(arcpy.env.workspace, TARGET) has an unknowable
    first half and a perfectly knowable dataset name at the end.
    """
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else UNKNOWN
    if isinstance(node, ast.Name):
        return env.get(node.id, UNKNOWN)
    if isinstance(node, ast.JoinedStr):
        return "".join(const_value(p, env) for p in node.values)
    if isinstance(node, ast.FormattedValue):
        # A conversion or a format spec can change the text in ways this does
        # not model, so the piece is unknown rather than guessed.
        if node.conversion not in (-1, None) or node.format_spec is not None:
            return UNKNOWN
        return const_value(node.value, env)
    if isinstance(node, ast.BinOp):
        if isinstance(node.op, ast.Add):
            return const_value(node.left, env) + const_value(node.right, env)
        return UNKNOWN
    if isinstance(node, ast.Call):
        if _os_join(node.func) and not node.keywords:
            parts = []
            for arg in node.args:
                if isinstance(arg, ast.Starred):
                    return UNKNOWN
                parts.append(const_value(arg, env))
            return join_paths(parts)
        return UNKNOWN
    if _os_sep(node):
        return "\\"
    return UNKNOWN


def tool_key(func, roots):
    """The table key for an arcpy call, or None when the call is not one.

    The chain has to start at a name that came from an arcpy import. Without
    that check a local helper called delete() is reported as a dropped dataset.
    """
    parts = []
    node = func
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    parts.reverse()
    if parts[0] not in roots:
        return None
    name = parts[-1]
    key = name
    for suffix in TOOL_SUFFIXES:
        if key.lower().endswith(suffix):
            key = key[:-len(suffix)]
            break
    key = key.lower()
    if key not in TOOLS:
        return None
    return key, name


def arcpy_roots(tree):
    """Names that stand for arcpy in this file, including aliases."""
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "arcpy" or alias.name.startswith("arcpy."):
                    roots.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "arcpy" or module.startswith("arcpy."):
                for alias in node.names:
                    roots.add(alias.asname or alias.name)
    return roots


class Site(object):
    """One arcpy call site, with the dataset it acts on and the verdict."""

    def __init__(self, path, verdict, tool, role, filename, line, via=None,
                 detail=None):
        self.path = path
        self.verdict = verdict
        self.tool = tool
        self.role = role
        self.file = filename
        self.line = line
        self.via = via
        self.detail = detail

    def __repr__(self):
        return "Site(%s %s(%s) %s:%d %s)" % (
            self.verdict, self.tool, self.role, self.file, self.line,
            render(self.path))


class _Ctx(object):
    """What one pass over a file collects."""

    def __init__(self, filename, roots, defs):
        self.file = filename
        self.roots = roots
        self.defs = defs
        self.sites = []
        self.layers = {}
        self.calls = []


def _arg_of(node, role, index):
    """The argument for one tool parameter, keyword form or positional."""
    for kw in node.keywords:
        if kw.arg == role:
            return kw.value
    if index < len(node.args):
        arg = node.args[index]
        if isinstance(arg, ast.Starred):
            return None
        return arg
    return None


def _register_layer(ctx, key, node, env, source):
    """Record layer name -> dataset it was made from, for this file."""
    role, index = LAYER_MAKERS[key]
    arg = _arg_of(node, role, index)
    if arg is None:
        return
    name = const_value(arg, env)
    if UNKNOWN in name or not name:
        return
    if name in ctx.layers and ctx.layers[name] != source:
        # The same layer name built from two different datasets. Resolving it
        # either way would name the wrong feature class, so it resolves to
        # nothing and the call site is reported as unresolved instead.
        ctx.layers[name] = UNKNOWN
        return
    ctx.layers[name] = source


def handle_call(node, env, ctx, layers):
    """Classify one Call node and record it if it is an arcpy data call."""
    name = None
    if isinstance(node.func, ast.Name):
        name = node.func.id
    elif isinstance(node.func, ast.Attribute):
        name = node.func.attr
    if name in ctx.defs:
        # A call to a function defined in this file. Its resolved arguments
        # are what lets a path reach a parameter.
        positional = []
        for arg in node.args:
            if isinstance(arg, ast.Starred):
                break
            positional.append(const_value(arg, env))
        keywords = dict((kw.arg, const_value(kw.value, env))
                        for kw in node.keywords if kw.arg)
        ctx.calls.append((name, positional, keywords))

    found = tool_key(node.func, ctx.roots)
    if found is None:
        return
    key, written = found
    first = None
    for role, index, verdict in TOOLS[key]:
        arg = _arg_of(node, role, index)
        if arg is None:
            continue
        path = const_value(arg, env)
        via = None
        if path and UNKNOWN not in path and path in layers:
            via = path
            path = layers[path]
        if first is None:
            first = path
        detail = None
        if key == "delete":
            kind = _arg_of(node, "data_type", 1)
            if kind is not None:
                text = const_value(kind, env)
                if UNKNOWN not in text:
                    detail = text
        ctx.sites.append(Site(path, verdict, written, role, ctx.file,
                              node.lineno, via, detail))
    if key in LAYER_MAKERS and first is not None:
        _register_layer(ctx, key, node, env, first)


def visit_calls(node, env, ctx, layers):
    """Every call inside one expression or simple statement, in source order."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            handle_call(sub, env, ctx, layers)


def _bodies(node):
    """The statement lists a compound statement owns, in execution order."""
    out = []
    for field in ("body", "orelse", "finalbody"):
        block = getattr(node, field, None)
        if isinstance(block, list) and block and isinstance(block[0], ast.stmt):
            out.append(block)
    for handler in getattr(node, "handlers", []) or []:
        out.append(handler.body)
    return out


def _heads(node):
    """The expressions a compound statement evaluates before its body."""
    out = []
    for field in ("test", "iter"):
        value = getattr(node, field, None)
        if value is not None:
            out.append(value)
    for item in getattr(node, "items", []) or []:
        out.append(item.context_expr)
    return out


def walk_body(body, env, ctx, layers):
    """Walk statements in source order, keeping a name to path environment.

    Assignment is sequential: the last value assigned before a call is the
    value that call sees.

    # ponytail: straight-line resolution, no branch merge. A name assigned
    # differently in two arms of an if resolves to whichever arm the walk saw
    # last. Upgrade path is a per-name set of candidate values and a site that
    # reports all of them.
    """
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            continue
        if isinstance(node, ast.Assign):
            visit_calls(node.value, env, ctx, layers)
            value = const_value(node.value, env)
            for target in node.targets:
                if isinstance(target, ast.Name):
                    env[target.id] = value
            continue
        if isinstance(node, ast.AugAssign):
            visit_calls(node.value, env, ctx, layers)
            if isinstance(node.target, ast.Name) and isinstance(node.op, ast.Add):
                env[node.target.id] = (env.get(node.target.id, UNKNOWN)
                                       + const_value(node.value, env))
            continue
        blocks = _bodies(node)
        if blocks:
            for head in _heads(node):
                visit_calls(head, env, ctx, layers)
            for block in blocks:
                walk_body(block, env, ctx, layers)
            continue
        visit_calls(node, env, ctx, layers)


def collect_defs(tree):
    """Every function in the file, by name and in source order.

    Two functions with one name cannot be told apart at a call site, so the
    name maps to None and neither receives a parameter binding. Both are still
    walked, because an ambiguous name is a reason to resolve less, never a
    reason to stop looking for the truncate inside.
    """
    defs = {}
    order = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            order.append(node)
            if node.name in defs:
                defs[node.name] = None
            else:
                defs[node.name] = node
    order.sort(key=lambda n: n.lineno)
    return defs, order


def derive_bindings(calls, defs):
    """Bind a parameter to a path when every call site passes the same one.

    This is what carries a module constant through a call into the function
    that truncates something.

    # ponytail: one binding per pass, capped at MAX_BINDING_PASSES, and no
    # tracking of self attributes. A chain of four calls, or a path held on
    # self.workspace, stays unresolved and is reported as such. Upgrade path
    # is a real call graph, walked callers first, instead of a fixed count of
    # replays.
    """
    out = {}
    for name, node in defs.items():
        if node is None:
            continue
        recorded = [c for c in calls if c[0] == name]
        if not recorded:
            continue
        params = [a.arg for a in node.args.args]
        if params and params[0] in ("self", "cls"):
            params = params[1:]
        seen = {}
        for _, positional, keywords in recorded:
            for index, value in enumerate(positional):
                if index < len(params):
                    seen.setdefault(params[index], set()).add(value)
            for key, value in keywords.items():
                if key in params:
                    seen.setdefault(key, set()).add(value)
        binding = {}
        for param, values in seen.items():
            if len(values) == 1:
                value = list(values)[0]
                if value != UNKNOWN:
                    binding[param] = value
        if binding:
            out[name] = binding
    return out


def scan_source(text, filename):
    """Every classified arcpy call site in one Python source file.

    Two passes. The first resolves module constants and records what each
    call site passes to the file's own functions. The second replays the file
    with those parameter bindings in place, and its sites are the answer.
    """
    # ast.parse compiles, and compiling a file with "C:\GIS" in a plain string
    # raises SyntaxWarning: invalid escape sequence. That is a real defect in
    # the file being read, but it is not this tool's finding, and printing it
    # interleaves warnings through the report. The file's own linter owns it.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        tree = ast.parse(text, filename)
    roots = arcpy_roots(tree)
    defs, order = collect_defs(tree)

    def replay(bindings, layers=None):
        ctx = _Ctx(filename, roots, defs)
        module_env = {}
        walk_body(tree.body, module_env, ctx, layers if layers else ctx.layers)
        for node in order:
            env = dict(module_env)
            # An ambiguous name maps to None, so only a function that owns its
            # name is given the arguments its callers passed.
            if defs.get(node.name) is node:
                env.update(bindings.get(node.name, {}))
            walk_body(node.body, env, ctx, layers if layers else ctx.layers)
        return ctx

    bindings = {}
    layers = {}
    for _ in range(MAX_BINDING_PASSES):
        ctx = replay(bindings)
        layers = ctx.layers
        found = derive_bindings(ctx.calls, defs)
        if found == bindings:
            break
        bindings = found

    final = replay(bindings, layers)
    # Stable sort on the line alone, so two sites on one line keep the order
    # the tool's own parameters are declared in: inputs before target.
    final.sites.sort(key=lambda s: s.line)
    return final.sites


# -------------------------------------------------------------- the report

def site_line(site):
    """One report line for a call site, and one for its resolved path."""
    head = "  %-6s %s:%d  %s(%s)" % (site.verdict, site.file, site.line,
                                     site.tool, site.role)
    if site.detail:
        head += " data_type=%s" % site.detail
    if site.via:
        head += " via layer %s" % site.via
    return [head, "         %s" % render(site.path)]


def report_lines(sites, query=None, destructive_only=False):
    """The whole report: grouped call sites, then the verdict, then caveats."""
    lines = []
    selected = [s for s in sites if query is None or matches(s.path, query)]
    if destructive_only:
        selected = [s for s in selected if s.verdict in DESTRUCTIVE]

    groups = {}
    for site in selected:
        groups.setdefault(dataset(site.path) or NO_NAME, []).append(site)
    for name in sorted(groups, key=lambda n: (
            min(SEVERITY[s.verdict] for s in groups[n]), n.lower())):
        lines.append(name)
        for site in sorted(groups[name],
                           key=lambda s: (SEVERITY[s.verdict], s.file, s.line)):
            lines.extend(site_line(site))
        lines.append("")

    destroyers = [s for s in selected if s.verdict in DESTRUCTIVE]
    if query is not None:
        if destroyers:
            lines.append("DESTROYED BY:")
            for site in sorted(destroyers, key=lambda s: (s.file, s.line)):
                lines.append("  %s:%d  %s  (%s)"
                             % (site.file, site.line, site.tool, site.verdict))
        else:
            lines.append("DESTROYED BY: nothing in the scanned files empties "
                         "or drops %s" % query)
        # A file that destroys the dataset is never listed as a reader, so the
        # read-only list is safe to act on.
        guilty = set(s.file for s in destroyers)
        readers = sorted(set(s.file for s in selected
                             if s.verdict == READ and s.file not in guilty))
        if readers:
            lines.append("READ ONLY: %s" % ", ".join(readers))

    unresolved = [s for s in sites
                  if s.verdict in DESTRUCTIVE and dataset(s.path) is None]
    if unresolved:
        lines.append("")
        lines.append("%d destructive call site(s) whose target could not be "
                     "resolved. The answer above is incomplete:"
                     % len(unresolved))
        for site in sorted(unresolved, key=lambda s: (s.file, s.line)):
            lines.extend(site_line(site))
    return lines


def exit_code(sites, query, destructive_only, errors):
    """0 nothing destructive reported, 1 something was, 2 the answer is partial."""
    if errors:
        return 2
    selected = [s for s in sites if query is None or matches(s.path, query)]
    if destructive_only:
        selected = [s for s in selected if s.verdict in DESTRUCTIVE]
    return 1 if any(s.verdict in DESTRUCTIVE for s in selected) else 0


# -------------------------------------------------------------- the io layer

def walk_files(paths):
    """Every source file under the given files and directories, sorted."""
    out = []
    for path in paths:
        if os.path.isfile(path):
            out.append(path)
            continue
        for root, dirs, files in os.walk(path):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
            for name in sorted(files):
                if name.lower().endswith(SOURCE_SUFFIXES):
                    out.append(os.path.join(root, name))
    return out


def read_source(path):
    """The text of a source file, or None when it cannot be read.

    utf-8-sig drops the byte order mark ArcGIS Pro's editor writes, which
    ast.parse otherwise reports as a syntax error on line 1. Undecodable bytes
    are replaced rather than fatal, because a stray cp1252 character in a
    comment must not hide the truncate on the line below it.
    """
    try:
        if os.path.getsize(path) > MAX_SOURCE_BYTES:
            return None
        handle = io.open(path, "r", encoding="utf-8-sig", errors="replace")
    except (IOError, OSError):
        return None
    try:
        return handle.read()
    except (IOError, OSError):
        return None
    finally:
        handle.close()


def scan_paths(paths):
    """Scan every source file under paths. Returns (sites, errors)."""
    sites = []
    errors = []
    for path in walk_files(paths):
        text = read_source(path)
        if text is None:
            errors.append((path, "could not be read"))
            continue
        try:
            sites.extend(scan_source(text, path))
        except SyntaxError as exc:
            errors.append((path, "line %s: %s" % (exc.lineno, exc.msg)))
        except (ValueError, RecursionError) as exc:
            # ast.parse raises ValueError on a null byte in the source, and a
            # generated file can nest deep enough to exhaust the stack.
            errors.append((path, "could not be parsed: %s" % exc))
    return sites, errors


# ------------------------------------------------------------- the self-test

def self_test():
    """Assertions over the resolver, the classifier, the report and the cli.

    Every fixture below is written for this test. Nothing here reads a real
    script, imports arcpy, opens a geodatabase or touches the network.
    """
    passed = [0]
    failed = []

    def check(cond, label):
        if cond:
            passed[0] += 1
            print("PASS  %s" % label)
        else:
            failed.append(label)
            print("FAIL  %s" % label)

    def raises(fn, label):
        try:
            fn()
        except SyntaxError:
            check(True, label)
        except Exception as exc:
            check(False, "%s (wrong exception %r)" % (label, exc))
        else:
            check(False, "%s (no error raised)" % label)

    def scan(source, filename="fixture.py"):
        return scan_source(textwrap.dedent(source), filename)

    def one(source):
        found = scan(source)
        return found[0] if found else None

    def summary(source):
        return [(s.verdict, s.tool, render(s.path)) for s in scan(source)]

    print("whowrites self-test: no arcpy, no geodatabase, no network")
    print("-" * 68)

    # ---- resolving a path that was assembled somewhere else
    loader = """
    import os
    import arcpy

    SDE = r"D:\\gis\\utility.sde"
    HYDRANTS = os.path.join(SDE, r"WATERWORKS.PUBLIC.Water\\WATERWORKS.PUBLIC.Hydrants")

    def refresh(staged):
        arcpy.management.GetCount(HYDRANTS)
        arcpy.TruncateTable_management(HYDRANTS)
        arcpy.Append_management(staged, HYDRANTS, "NO_TEST")
    """
    sites = scan(loader)
    truncates = [s for s in sites if s.verdict == EMPTY]
    check(len(truncates) == 1, "one truncate is found in the loader fixture")
    check(truncates[0].path.endswith("WATERWORKS.PUBLIC.Hydrants"),
          "a constant joined at module level resolves inside a function"
          "  <-- pinned defect")
    check(UNKNOWN not in truncates[0].path,
          "the resolved truncate path has no unknown piece left in it")
    check(dataset(truncates[0].path) == "WATERWORKS.PUBLIC.Hydrants",
          "the dataset name is the last segment of the joined path")
    check([s.verdict for s in sites if s.tool == "Append_management"]
          == [READ, WRITE],
          "Append reads its inputs and writes its target, in that order")
    check(len([s for s in sites if s.verdict == READ]) == 2,
          "GetCount and the Append input are the two reads")

    # ---- the keyword form and the positional form agree
    positional = one("""
    import arcpy
    arcpy.management.DeleteRows(r"D:\\gis\\water.gdb\\Mains")
    """)
    keyword = one("""
    import arcpy
    arcpy.management.DeleteRows(in_rows=r"D:\\gis\\water.gdb\\Mains")
    """)
    check(positional.verdict == EMPTY and positional.role == "in_rows",
          "DeleteRows names its first positional argument in_rows")
    check(keyword.verdict == positional.verdict
          and keyword.path == positional.path,
          "the keyword form resolves like the positional form"
          "  <-- pinned defect")
    kw_append = summary("""
    import arcpy
    arcpy.management.Append(inputs=r"D:\\gis\\stage.gdb\\Mains",
                            target=r"D:\\gis\\water.gdb\\Mains",
                            schema_type="NO_TEST")
    """)
    check((WRITE, "Append", "D:\\gis\\water.gdb\\Mains") in kw_append,
          "Append by keyword writes the target, not the inputs")
    check((READ, "Append", "D:\\gis\\stage.gdb\\Mains") in kw_append,
          "Append by keyword reads the inputs, not the target")
    spelled = summary("""
    import arcpy
    arcpy.Append_management(r"D:\\gis\\stage.gdb\\Mains",
                            r"D:\\gis\\water.gdb\\Mains", "NO_TEST")
    """)
    check([(v, p) for v, _, p in spelled] == [(v, p) for v, _, p in kw_append],
          "the two spellings of Append produce the same two verdicts")

    # ---- object level destruction is its own verdict
    dropped = one("""
    import arcpy
    arcpy.Delete_management(r"D:\\gis\\water.gdb\\Mains", "FeatureClass")
    """)
    check(dropped.verdict == DROP,
          "Delete with a data type drops the object, it does not empty it"
          "  <-- pinned defect")
    check(dropped.detail == "FeatureClass",
          "the data type argument is carried into the report")
    check(one("""
    import arcpy
    arcpy.Delete_management(r"D:\\gis\\water.gdb\\Mains")
    """).detail is None,
          "Delete without a data type reports no data type")
    check(one("""
    import arcpy
    arcpy.management.DeleteFeatures(r"D:\\gis\\water.gdb\\Mains")
    """).verdict == EMPTY,
          "DeleteFeatures empties the rows and leaves the object")
    check(SEVERITY[DROP] < SEVERITY[EMPTY] < SEVERITY[WRITE] < SEVERITY[READ],
          "the verdicts sort worst first")

    # ---- THE PINNED DEFECT: a reader is not the thing destroyed
    compare = """
    import arcpy

    TODAY = r"D:\\gis\\utility.sde\\WATERWORKS.PUBLIC.Hydrants"
    CHANGES = r"D:\\gis\\utility.sde\\WATERWORKS.PUBLIC.HydrantsChanges"

    arcpy.TruncateTable_management(CHANGES)
    with arcpy.da.SearchCursor(TODAY, ["OBJECTID"]) as cursor:
        pass
    """
    sites = scan(compare, "compare.py")
    hydrants = [s for s in sites if matches(s.path, "Hydrants")]
    changes = [s for s in sites if matches(s.path, "HydrantsChanges")]
    check([s.verdict for s in hydrants] == [READ],
          "a script that reads Hydrants and truncates HydrantsChanges is a "
          "READER of Hydrants  <-- pinned defect")
    check([s.verdict for s in changes] == [EMPTY],
          "the same script is still the destroyer of HydrantsChanges"
          "  <-- pinned defect")
    check(not matches(r"D:\gis\w.sde\WATERWORKS.PUBLIC.HydrantsChanges",
                      "Hydrants"),
          "HydrantsChanges never answers to the name Hydrants")
    check(not matches(r"D:\gis\w.sde\WATERWORKS.PUBLIC.Hydrants",
                      "HydrantsChanges"),
          "Hydrants never answers to the name HydrantsChanges")
    check(matches(r"D:\gis\w.sde\WATERWORKS.PUBLIC.Hydrants", "hydrants"),
          "a name query ignores case")
    check(not matches(r"D:\gis\w.sde\WATERWORKS.PUBLIC.Hydrants", "Hydrant"),
          "a shorter name is not a prefix match")
    check(matches(r"D:\gis\w.sde\WATERWORKS.PUBLIC.Hydrants", "PUBLIC.Hydrants"),
          "an owner qualified query matches the owner qualified name")
    check(not matches(r"D:\gis\w.sde\WATERWORKS.PUBLIC.Hydrants", "PUBLIC"),
          "the owner alone is not a dataset name")
    check(matches(r"D:\gis\water\Hydrants.shp", "Hydrants"),
          "a shapefile answers to its name without the extension")
    check(names(r"D:\gis\w.sde\WATERWORKS.PUBLIC.Hydrants")
          == ("WATERWORKS.PUBLIC.Hydrants", "Hydrants", "PUBLIC.Hydrants"),
          "an owner qualified feature class answers to three names")

    # ---- resolving concatenation, f-strings and the unknown
    check(dataset(one("""
    import arcpy
    SDE = r"D:\\gis\\utility.sde"
    OUT = SDE + r"\\WATERWORKS.PUBLIC.ServiceAreas"
    arcpy.management.TruncateTable(OUT)
    """).path) == "WATERWORKS.PUBLIC.ServiceAreas",
          "a path built by string concatenation resolves")
    check(dataset(one("""
    import arcpy
    BASE = r"D:\\gis"
    NAME = "Hydrants"
    arcpy.management.TruncateTable(f"{BASE}\\\\water.gdb\\\\{NAME}")
    """).path) == "Hydrants",
          "an f-string of resolved names resolves")
    partial = one("""
    import arcpy
    import os
    arcpy.management.TruncateTable(os.path.join(arcpy.env.workspace,
                                                r"WORK.PUBLIC.Mains"))
    """)
    check(dataset(partial.path) == "WORK.PUBLIC.Mains",
          "an unknown workspace still leaves a knowable dataset name")
    check(render(partial.path) == "?\\WORK.PUBLIC.Mains",
          "the unknown half of the path prints as a question mark")
    tail = one("""
    import arcpy
    def go(prefix):
        arcpy.management.TruncateTable(prefix + "Mains")
    """)
    check(dataset(tail.path) is None,
          "an unknown prefix glued to a name resolves to no name at all"
          "  <-- pinned defect")
    # The dataset name comes out "Mains" with or without the reset, so
    # this one asserts the whole path. Dropped, the path reads
    # D:\gis\D:\other.gdb\Mains and sends an operator to the wrong
    # database.
    check(render(one("""
    import arcpy
    import os
    BASE = "D:\\\\gis"
    arcpy.management.TruncateTable(os.path.join(BASE, r"D:\\other.gdb\\Mains"))
    """).path) == r"D:\other.gdb\Mains",
          "an absolute second segment resets the join, as os.path.join does")
    check(dataset(one("""
    import arcpy
    P = "D:/gis/water.gdb/Mains"
    arcpy.management.TruncateTable(P)
    """).path) == "Mains",
          "a forward slash path is split like a backslash path")
    check(dataset(one("""
    import arcpy
    P = "D:/gis/water.gdb/Mains/"
    arcpy.management.TruncateTable(P)
    """).path) == "Mains",
          "a trailing separator does not hide the dataset name")
    check(dataset(one("""
    import arcpy
    P = "%s/Mains" % BASE
    arcpy.management.TruncateTable(P)
    """).path) is None,
          "percent formatting is not resolved, and says so")
    check(dataset(one("""
    import arcpy
    N = "Mains"
    arcpy.management.TruncateTable(f"D:/gis/{N:>12}")
    """).path) is None,
          "an f-string format spec makes the piece unknown, not a guess")
    check(dataset(one("""
    import arcpy
    P = r"D:\\gis\\water.gdb\\Old"
    P = r"D:\\gis\\water.gdb\\New"
    arcpy.management.TruncateTable(P)
    """).path) == "New",
          "the last assignment before the call is the one the call sees")
    check(dataset(one("""
    import arcpy
    P = r"D:\\gis\\water.gdb"
    P += "\\\\Mains"
    arcpy.management.TruncateTable(P)
    """).path) == "Mains",
          "an augmented assignment extends the path")
    check(render(one("""
    import arcpy
    import os
    arcpy.management.TruncateTable(os.path.join("D:\\\\gis", "w.gdb" + os.sep + "Mains"))
    """).path) == r"D:\gis\w.gdb\Mains",
          "os.sep is read as a separator and written back out as a backslash")

    # ---- a path that reaches a truncate through a parameter
    through_param = """
    import os
    import arcpy

    WORKSPACE = r"D:\\gis\\utility.sde"
    TARGET = r"WATERWORKS.PUBLIC.Water\\WATERWORKS.PUBLIC.Buffer500"

    class Job(object):
        def replace(self, source, target_path):
            full = os.path.join(WORKSPACE, target_path)
            arcpy.management.DeleteFeatures(full)
            arcpy.management.Append(source, full, "NO_TEST")

        def run(self, source):
            self.replace(source, TARGET)
    """
    sites = scan(through_param, "job.py")
    emptied = [s for s in sites if s.verdict == EMPTY]
    check(dataset(emptied[0].path) == "WATERWORKS.PUBLIC.Buffer500",
          "a constant passed into a method reaches the DeleteFeatures inside it")
    check(len([s for s in sites if s.verdict == WRITE]) == 1,
          "the Append inside the same method writes one target")
    two_hops = """
    import os
    import arcpy

    WORKSPACE = r"D:\\gis\\utility.sde"
    TARGET = r"WATERWORKS.PUBLIC.Water\\WATERWORKS.PUBLIC.Buffer500"

    class Job(object):
        def replace(self, target_path):
            arcpy.management.DeleteFeatures(os.path.join(WORKSPACE, target_path))

        def process(self, target_fc):
            self.replace(target_fc)

    def run():
        Job().process(TARGET)
    """
    check(dataset(scan(two_hops)[0].path) == "WATERWORKS.PUBLIC.Buffer500",
          "a path carried through two calls still reaches the delete"
          "  <-- pinned defect")
    deep_chain = """
    import arcpy
    A = r"D:\\gis\\water.gdb\\Mains"

    def hop1(p):
        arcpy.management.TruncateTable(p)

    def hop2(p):
        hop1(p)

    def hop3(p):
        hop2(p)

    def hop4(p):
        hop3(p)

    hop4(A)
    """
    check(dataset(scan(deep_chain)[0].path) is None,
          "a chain longer than MAX_BINDING_PASSES stays unresolved, not guessed")

    two_callers = """
    import arcpy
    A = r"D:\\gis\\water.gdb\\Mains"
    B = r"D:\\gis\\water.gdb\\Laterals"

    def wipe(path):
        arcpy.management.TruncateTable(path)

    wipe(A)
    wipe(B)
    """
    check(dataset(scan(two_callers)[0].path) is None,
          "a parameter passed two different paths binds to neither")
    check(dataset(scan("""
    import arcpy
    def wipe(path):
        arcpy.management.TruncateTable(path)
    """)[0].path) is None,
          "a function nobody calls has no parameter binding")
    check(dataset(scan("""
    import arcpy
    A = r"D:\\gis\\water.gdb\\Mains"

    def wipe(path):
        arcpy.management.TruncateTable(path)

    def wipe(path):
        arcpy.management.DeleteRows(path)

    wipe(A)
    """)[0].path) is None,
          "two functions with one name bind no parameters")
    check(dataset(scan("""
    import arcpy
    A = r"D:\\gis\\water.gdb\\Mains"

    def wipe(path=None):
        arcpy.management.TruncateTable(path)

    wipe(path=A)
    """)[0].path) == "Mains",
          "a keyword argument at the call site binds the parameter too")

    # ---- a layer name stands in for the feature class under it
    layered = """
    import arcpy

    MAINS = r"D:\\gis\\utility.sde\\WATERWORKS.PUBLIC.Mains"
    arcpy.management.MakeFeatureLayer(MAINS, "mains_lyr")
    arcpy.management.SelectLayerByAttribute("mains_lyr", "NEW_SELECTION", "1=1")
    arcpy.management.DeleteFeatures("mains_lyr")
    """
    sites = scan(layered, "layer.py")
    killed = [s for s in sites if s.verdict == EMPTY]
    check(dataset(killed[0].path) == "WATERWORKS.PUBLIC.Mains",
          "deleting from a layer is reported against the feature class"
          "  <-- pinned defect")
    check(killed[0].via == "mains_lyr",
          "the report still names the layer the call was written against")
    check(dataset(scan("""
    import arcpy
    A = r"D:\\gis\\water.gdb\\Mains"
    B = r"D:\\gis\\water.gdb\\Laterals"
    arcpy.management.MakeFeatureLayer(A, "lyr")
    arcpy.management.MakeFeatureLayer(B, "lyr")
    arcpy.management.DeleteFeatures("lyr")
    """)[-1].path) is None,
          "one layer name built from two datasets resolves to neither")

    # ---- what is not an arcpy call
    check(scan("""
    import shutil
    shutil.delete(r"D:\\gis\\water.gdb\\Mains")
    """) == [],
          "a delete that is not an arcpy call is not a finding")
    check(scan("""
    def delete(path):
        pass

    delete(r"D:\\gis\\water.gdb\\Mains")
    """) == [],
          "a local helper named delete is not arcpy.Delete")
    check(scan("""
    import arcpy
    arcpy.management.CalculateGeometryAttributes(r"D:\\gis\\w.gdb\\Mains", [])
    """) == [],
          "a tool that is not in the table is not classified")
    check(one("""
    import arcpy as ap
    ap.management.TruncateTable(r"D:\\gis\\water.gdb\\Mains")
    """).verdict == EMPTY,
          "an aliased arcpy import is still arcpy")
    check(one("""
    from arcpy import management
    management.TruncateTable(r"D:\\gis\\water.gdb\\Mains")
    """).verdict == EMPTY,
          "from arcpy import management is still arcpy")
    check(one("""
    from arcpy.da import SearchCursor
    SearchCursor(r"D:\\gis\\water.gdb\\Mains", ["OID@"])
    """).verdict == READ,
          "a cursor imported by name is still a read")
    check(one("""
    import arcpy
    arcpy.management.TruncateTable(*paths)
    """) is None,
          "a starred argument list names no dataset")

    # ---- calls found in every place a statement can hide one
    nested = """
    import arcpy
    P = r"D:\\gis\\water.gdb\\Mains"
    Q = r"D:\\gis\\water.gdb\\Laterals"
    if arcpy.Exists(P):
        for x in range(1):
            try:
                arcpy.management.TruncateTable(P)
            except Exception:
                arcpy.management.DeleteRows(Q)
            finally:
                arcpy.management.GetCount(P)
    else:
        while arcpy.Exists(Q):
            arcpy.management.DeleteFeatures(Q)
    """
    found = summary(nested)
    check(len(found) == 6,
          "calls in if, for, try, except, finally, else and while are all found")
    check([s[0] for s in found if s[0] in DESTRUCTIVE] == [EMPTY] * 3,
          "the three destructive calls in that nest are all reported")

    # ---- the report
    sites = scan(loader, "loader.py") + scan(compare, "compare.py")
    text = "\n".join(report_lines(sites, "Hydrants"))
    check("DESTROYED BY:\n  loader.py:10" in text,
          "the verdict names the file and line that truncates Hydrants")
    check("compare.py" not in text.split("DESTROYED BY:")[1].split(
          "READ ONLY:")[0],
          "the reader is not listed under DESTROYED BY  <-- pinned defect")
    check("READ ONLY: compare.py" in text,
          "the reader is listed as a reader")
    # loader.py reads Hydrants at its GetCount and truncates it two lines
    # later. An operator who acts on READ ONLY clears the one file that
    # actually emptied the layer, so the list holds readers and nothing
    # else.
    read_only = text.split("READ ONLY: ")[1].splitlines()[0]
    check(read_only == "compare.py",
          "a file that both reads and truncates Hydrants is kept out of "
          "the read only list  <-- pinned defect")
    check("HydrantsChanges" not in text,
          "a query for Hydrants does not report HydrantsChanges at all")
    quiet = "\n".join(report_lines(sites, "Laterals"))
    check("DESTROYED BY: nothing" in quiet,
          "a dataset nobody destroys says so in those words")
    check(exit_code(sites, "Hydrants", False, []) == 1,
          "a destroyed dataset exits 1")
    check(exit_code(sites, "Laterals", False, []) == 0,
          "a dataset with no destroyer exits 0")
    check(exit_code(sites, None, True, [("x.py", "boom")]) == 2,
          "a file that could not be parsed exits 2")
    only = report_lines(sites, None, True)
    check(not [ln for ln in only if "READ" in ln],
          "--destructive-only prints no read call sites")
    grouped = "\n".join(report_lines(scan("""
    import arcpy
    arcpy.da.SearchCursor(r"D:\\gis\\w.gdb\\Alpha", ["OID@"])
    arcpy.management.TruncateTable(r"D:\\gis\\w.gdb\\Zulu")
    """)))
    check(grouped.index("Zulu") < grouped.index("Alpha"),
          "with no name given the destroyed dataset sorts above the read one")
    check(NO_NAME in "\n".join(report_lines(scan("""
    import arcpy
    def wipe(path):
        arcpy.management.TruncateTable(path)
    """))),
          "a call site with no resolvable name is grouped under one that says so")
    check(exit_code(sites, None, True, []) == 1,
          "--destructive-only still exits 1 when it printed a destroyer")
    detailed = "\n".join(report_lines(scan("""
    import arcpy
    A = r"D:\\gis\\w.gdb\\Mains"
    arcpy.management.MakeFeatureLayer(A, "lyr")
    arcpy.management.DeleteFeatures("lyr")
    arcpy.Delete_management(A, "FeatureClass")
    """, "drop.py")))
    check("data_type=FeatureClass" in detailed,
          "the report says which kind of object a Delete removes")
    check("via layer lyr" in detailed,
          "the report says the call was written against a layer")
    caveat = "\n".join(report_lines(scan("""
    import arcpy
    def wipe(path):
        arcpy.management.TruncateTable(path)
    """), "Mains"))
    check("could not be resolved" in caveat,
          "an unresolved destructive call site is always reported as a caveat")

    # ---- corners of the resolver
    slashed = one("""
    import arcpy
    import os
    arcpy.management.TruncateTable(os.path.join("D:/gis/", "Mains"))
    """)
    check(render(slashed.path) == "D:/gis/Mains",
          "a base that already ends in a separator does not gain a second one")
    check(dataset(slashed.path) == "Mains",
          "that path still names its dataset")
    check("EMPTY" in repr(slashed) and "Mains" in repr(slashed),
          "a call site prints its verdict and its path")
    check(dataset(one("""
    import arcpy
    import os
    arcpy.management.TruncateTable("D:/gis" + os.path.sep + "Mains")
    """).path) == "Mains",
          "os.path.sep is read as a separator, like os.sep")
    check(dataset(one("""
    import arcpy
    P = "Mains" * 2
    arcpy.management.TruncateTable(P)
    """).path) is None,
          "a repeated string is not resolved to a path")
    check(dataset(one("""
    import arcpy
    def build():
        return "D:/gis/Mains"

    arcpy.management.TruncateTable(build())
    """).path) is None,
          "a path returned by a function call is not resolved")
    check(dataset(one("""
    import arcpy
    arcpy.management.TruncateTable("")
    """).path) is None,
          "an empty path names nothing")
    check(dataset(one("""
    import arcpy
    import os
    PARTS = []
    arcpy.management.TruncateTable(os.path.join(*PARTS))
    """).path) is None,
          "a join over a starred list of parts is not resolved")
    check(scan("""
    import arcpy
    get().management.TruncateTable("D:/gis/Mains")
    """) == [],
          "a tool reached through a function call is not classified")
    check(scan("""
    import arcpy
    HANDLERS = []
    HANDLERS[0]("D:/gis/Mains")
    """) == [],
          "a call through a subscript is not classified")
    check(one("""
    from os import path
    from arcpy import management
    management.TruncateTable("D:/gis/Mains")
    """).verdict == EMPTY,
          "an unrelated from-import does not confuse the arcpy check")
    check(dataset(one("""
    import arcpy
    d = {}
    d["p"] = "D:/gis/Mains"
    arcpy.management.TruncateTable(d["p"])
    """).path) is None,
          "a path stashed in a dictionary is not resolved")
    check(one("""
    import arcpy
    N = 3
    N *= 2
    arcpy.management.TruncateTable("D:/gis/Mains")
    """).verdict == EMPTY,
          "an augmented assignment that is not a path is ignored")
    check(one("""
    import arcpy
    A = "D:/gis/Mains"
    arcpy.management.MakeFeatureLayer(A)
    """).verdict == READ,
          "MakeFeatureLayer without an output name registers no layer")
    check(len(scan("""
    import arcpy
    def go(name):
        arcpy.management.MakeFeatureLayer("D:/gis/Mains", name)
        arcpy.management.DeleteFeatures(name)
    """)) == 2,
          "a layer whose name is unknown registers nothing")
    check(dataset(scan("""
    import arcpy
    ARGS = []

    def wipe(path):
        arcpy.management.TruncateTable(path)

    wipe(*ARGS)
    """)[0].path) is None,
          "a starred call site binds no parameter")
    dropped_kind = one("""
    import arcpy
    def go(kind):
        arcpy.Delete_management("D:/gis/Mains", kind)
    """)
    check(dropped_kind.verdict == DROP and dropped_kind.detail is None,
          "a Delete whose data type is unknown is still a DROP")
    check(dataset(scan("""
    import arcpy
    A = "D:/gis/Mains"

    def wipe(path):
        arcpy.management.TruncateTable(path)

    wipe(A, "extra")
    """)[0].path) == "Mains",
          "an argument with no parameter to land in is dropped, not misbound")
    check(dataset(scan("""
    import arcpy
    A = "D:/gis/Mains"

    def wipe(path):
        arcpy.management.TruncateTable(path)

    wipe(A, other="x")
    """)[0].path) == "Mains",
          "a keyword the function does not take is dropped, not misbound")

    # ---- a defect in the file being read is not this tool's output
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        escaped = one("""
    import arcpy
    arcpy.management.TruncateTable("D:\\gis\\Mains")
    """)
    check(dataset(escaped.path) == "Mains",
          "a path written without a raw string still resolves")
    check(caught == [],
          "an invalid escape sequence in the scanned file raises no warning "
          "out of this tool  <-- pinned defect")

    # ---- broken source
    raises(lambda: scan("def oops(:\n    pass\n"),
           "a file that does not parse raises SyntaxError to the caller")

    # ---- argument parsing
    args = _parse(["scripts", "--name", "Hydrants"])
    check(args.paths == ["scripts"] and args.name == "Hydrants",
          "a path and a name are parsed")
    check(_parse(["a", "b"]).paths == ["a", "b"],
          "more than one path is accepted")
    check(_parse(["x"]).name is None,
          "no name means report every dataset")
    check(_parse(["x"]).destructive_only is False,
          "reads are reported unless --destructive-only is given")
    check(_parse(["x", "--destructive-only"]).destructive_only is True,
          "--destructive-only is off until asked for")
    check(_parse(["--self-test"]).self_test is True,
          "--self-test is a flag")
    check(_parse(["x"]).self_test is False,
          "--self-test is off by default")

    # ---- a unique prefix of the longest option is refused, not guessed at
    stderr_saved = sys.stderr
    sys.stderr = io.StringIO()
    try:
        abbreviated = _parse(["x", "--destructive"])
    except SystemExit:
        abbreviated = None
    finally:
        sys.stderr = stderr_saved
    check(abbreviated is None,
          "a unique prefix of --destructive-only is refused by the parser  "
          "<-- pinned defect")

    # ---- the io layer, against files on disk
    import shutil
    import tempfile

    tmp = tempfile.mkdtemp(prefix="whowrites-selftest-")
    try:
        def write(name, body):
            path = os.path.join(tmp, name)
            handle = io.open(path, "w", encoding="utf-8")
            handle.write(textwrap.dedent(body))
            handle.close()
            return path

        write("loader.py", loader)
        write("compare.py", compare)
        write("notes.md", "# not source\n")
        write("broken.py", "def oops(:\n    pass\n")
        os.mkdir(os.path.join(tmp, "__pycache__"))
        write(os.path.join("__pycache__", "cached.py"), loader)

        files = walk_files([tmp])
        check(len(files) == 3,
              "the walker takes .py and skips .md and __pycache__")
        # A script share is full of toolboxes and of .py files saved
        # as .txt to clear a mail filter. Both still run, so both are
        # read.
        write("tools.pyt", loader)
        write("saved_as.txt", loader)
        widened = walk_files([tmp])
        check(sorted(os.path.basename(f) for f in widened)
              == ["broken.py", "compare.py", "loader.py", "saved_as.txt",
                  "tools.pyt"],
              "a .pyt toolbox and a .py saved as .txt are both walked")
        check(all(f.endswith(".py") for f in files),
              "every walked file is a source file")
        sites, errors = scan_paths([tmp])
        check(len(errors) == 1 and "line 1" in errors[0][1],
              "the unparsable file is reported once, with its line")
        check(any(s.verdict == EMPTY and dataset(s.path)
                  == "WATERWORKS.PUBLIC.Hydrants" for s in sites),
              "scanning a directory finds the truncate in it")
        check(exit_code(sites, "Hydrants", False, errors) == 2,
              "a run with an unparsable file is incomplete even with an answer")

        single = walk_files([os.path.join(tmp, "notes.md")])
        check(single == [os.path.join(tmp, "notes.md")],
              "a file named on the command line is scanned whatever it ends in")
        empty_sites, empty_errors = scan_paths([os.path.join(tmp, "notes.md")])
        check(empty_sites == [] and empty_errors == [],
              "a markdown file parses as Python and holds no call sites")

        big = write("big.py", "x = 1\n")
        old_limit = globals()["MAX_SOURCE_BYTES"]
        globals()["MAX_SOURCE_BYTES"] = 1
        check(read_source(big) is None,
              "a file over the size limit is not read")
        globals()["MAX_SOURCE_BYTES"] = old_limit
        check(read_source(os.path.join(tmp, "missing.py")) is None,
              "a file that is not there reads as None, not a traceback")
        globals()["MAX_SOURCE_BYTES"] = 1
        oversize = scan_paths([big])
        globals()["MAX_SOURCE_BYTES"] = old_limit
        check(oversize[1] == [(big, "could not be read")],
              "a file the reader refuses is an error, never a clean answer")

        nulled = os.path.join(tmp, "nulled.py")
        handle = io.open(nulled, "wb")
        handle.write(b"import arcpy\nA = 'a\x00b'\n")
        handle.close()
        # Python 3.9 raises ValueError here and 3.13 raises SyntaxError, so
        # the assertion is on the message both of them carry.
        check("null bytes" in scan_paths([nulled])[1][0][1],
              "a null byte in the source is an error, not a traceback")

        deep = write("deep.py", "import arcpy\nP = %s\n"
                     "arcpy.management.TruncateTable(P)\n"
                     % " + ".join(['"a"'] * 3000))
        check("could not be parsed" in scan_paths([deep])[1][0][1],
              "a file that nests deeper than the parser is an error, not a "
              "traceback  <-- pinned defect")

        bom = os.path.join(tmp, "bom.py")
        handle = io.open(bom, "wb")
        handle.write(b"\xef\xbb\xbfimport arcpy\narcpy.Delete_management('a')\n")
        handle.close()
        check(scan_paths([bom])[0][0].verdict == DROP,
              "a byte order mark does not break the parse  <-- pinned defect")

        # main() prints a whole report and its errors. This transcript is a
        # list of assertions, so both streams are captured while the five
        # exit codes are collected, the same way the harness probe below
        # captures its own deliberate failures.
        quiet, loud = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
        try:
            codes = [main([tmp, "--name", "Hydrants"]),
                     main([os.path.join(tmp, "loader.py"),
                           "--name", "Hydrants"]),
                     main([os.path.join(tmp, "compare.py"),
                           "--name", "Hydrants"]),
                     main([]),
                     main([os.path.join(tmp, "nope")])]
        finally:
            sys.stdout, sys.stderr = quiet, loud
        check(codes[0] == 2,
              "main reports 2 when one file in the directory is broken")
        check(codes[1] == 1,
              "main reports 1 when it names a destroyer")
        check(codes[2] == 0,
              "main reports 0 when only readers touch the dataset")
        check(codes[3] == 64, "main with no path is a usage error")
        check(codes[4] == 64,
              "main with a path that does not exist is a usage error")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    check(not os.path.isdir(tmp), "the self-test leaves no directory behind")

    # ---- the harness itself. A check() that cannot record a failure would
    # report every assertion above as a pass, which is the one defect none of
    # them could see. Three deliberate failures are recorded against a scratch
    # mark and then taken back off the tally.
    quiet = sys.stdout
    sys.stdout = io.StringIO()
    mark = len(failed)
    try:
        check(False, "probe: a false condition must be recorded as a failure")
        raises(lambda: None, "probe: a call that raises nothing must fail")
        raises(lambda: [][0], "probe: the wrong exception must fail")
    finally:
        sys.stdout = quiet
    probe = failed[mark:]
    del failed[mark:]
    check(len(probe) == 3,
          "check() and raises() really do record a failure  <-- pinned defect")

    print("-" * 68)
    total = passed[0] + len(failed)
    if failed:
        print("%d assertions, %d failed" % (total, len(failed)))
        for label in failed:
            print("  FAILED: %s" % label)
        return 1
    print("%d assertions, 0 failed" % total)
    return 0


# ----------------------------------------------------------------------- cli

def _parse(argv):
    parser = argparse.ArgumentParser(
        prog="whowrites.py",
        description="Name every call site that empties, drops or rewrites a "
                    "feature class, and never name a reader as the thing "
                    "destroyed.",
        epilog="This tool reads source code. It changes nothing.",
        allow_abbrev=False,
    )
    parser.add_argument("paths", nargs="*",
                        help="Python files or directories of them to read")
    parser.add_argument("--name", default=None, metavar="DATASET",
                        help="report only call sites against this dataset "
                             "name. Owner qualified or not, case insensitive.")
    parser.add_argument("--destructive-only", dest="destructive_only",
                        action="store_true",
                        help="report only the call sites that empty or drop "
                             "a dataset")
    parser.add_argument("--self-test", dest="self_test", action="store_true",
                        help="run the assertions and exit")
    return parser.parse_args(argv)


def main(argv=None):
    args = _parse(sys.argv[1:] if argv is None else argv)

    if args.self_test:
        return self_test()

    if not args.paths:
        print("error: pass one or more Python files or directories. Use "
              "--self-test to check the tool without any.", file=sys.stderr)
        return 64
    missing = [p for p in args.paths if not os.path.exists(p)]
    if missing:
        # A typo would otherwise report a clean answer over nothing at all,
        # which is the worst possible reply to "who emptied this layer".
        print("error: no such file or directory: %s" % ", ".join(missing),
              file=sys.stderr)
        return 64

    files = walk_files(args.paths)
    sites, errors = scan_paths(args.paths)
    print("whowrites: %d file(s), %d call site(s)" % (len(files), len(sites)))
    print("")
    for line in report_lines(sites, args.name, args.destructive_only):
        print(line)
    for path, message in errors:
        print("error: %s: %s" % (path, message), file=sys.stderr)
    return exit_code(sites, args.name, args.destructive_only, errors)


if __name__ == "__main__":
    sys.exit(main())
