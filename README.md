# whowrites

Name every call site that empties, drops or rewrites a feature class, and never name a reader as the thing destroyed.

The published hydrant layer comes up empty on a Monday morning. The geodatabase records nothing
about who wrote to it. The scheduled task recorded a command line and an exit code. The only
lineage anybody has is the script share, so the admin greps it for `Hydrants` and gets five files.

Four of those five only read the layer. The fifth truncated it, and the truncate line does not
contain the word `Hydrants` at all:

```python
SDE = r"D:\gis\utility.sde"
HYDRANTS = os.path.join(SDE, r"WATERWORKS.PUBLIC.Assets\WATERWORKS.PUBLIC.Hydrants")
...
arcpy.TruncateTable_management(HYDRANTS)      # 300 lines later
```

So the admin reads five scripts at 07:40 and picks the wrong one, because the report script also
calls `TruncateTable` and grep put it in the same list. It truncates a different feature class:
`WATERWORKS.PUBLIC.HydrantsChanges`, its own scratch table. Getting that backwards costs the
morning in either direction. Accuse the reader and the real job runs again tonight. Miss the
destroyer and nobody knows what emptied the layer.

This tool parses the scripts, resolves the paths they build, and classifies each call site.

```
$ python whowrites.py --self-test
whowrites self-test: no arcpy, no geodatabase, no network
--------------------------------------------------------------------
PASS  one truncate is found in the loader fixture
PASS  a constant joined at module level resolves inside a function  <-- pinned defect
PASS  the resolved truncate path has no unknown piece left in it
PASS  the dataset name is the last segment of the joined path
PASS  Append reads its inputs and writes its target, in that order
...
PASS  the keyword form resolves like the positional form  <-- pinned defect
PASS  Append by keyword writes the target, not the inputs
PASS  Append by keyword reads the inputs, not the target
PASS  the two spellings of Append produce the same two verdicts
PASS  Delete with a data type drops the object, it does not empty it  <-- pinned defect
PASS  the data type argument is carried into the report
...
PASS  DeleteFeatures empties the rows and leaves the object
...
PASS  a script that reads Hydrants and truncates HydrantsChanges is a READER of Hydrants  <-- pinned defect
PASS  the same script is still the destroyer of HydrantsChanges  <-- pinned defect
PASS  HydrantsChanges never answers to the name Hydrants
PASS  Hydrants never answers to the name HydrantsChanges
...
PASS  an unknown prefix glued to a name resolves to no name at all  <-- pinned defect
...
PASS  the last assignment before the call is the one the call sees
...
PASS  a constant passed into a method reaches the DeleteFeatures inside it
...
PASS  a path carried through two calls still reaches the delete  <-- pinned defect
PASS  a chain longer than MAX_BINDING_PASSES stays unresolved, not guessed
PASS  a parameter passed two different paths binds to neither
PASS  a function nobody calls has no parameter binding
...
PASS  deleting from a layer is reported against the feature class  <-- pinned defect
...
PASS  one layer name built from two datasets resolves to neither
...
PASS  a local helper named delete is not arcpy.Delete
...
PASS  the verdict names the file and line that truncates Hydrants
PASS  the reader is not listed under DESTROYED BY  <-- pinned defect
PASS  the reader is listed as a reader
PASS  a file that both reads and truncates Hydrants is kept out of the read only list  <-- pinned defect
PASS  a query for Hydrants does not report HydrantsChanges at all
...
PASS  an unresolved destructive call site is always reported as a caveat
...
PASS  an invalid escape sequence in the scanned file raises no warning out of this tool  <-- pinned defect
...
PASS  a path resolved from literals is PROVEN, and a read is not graded
...
PASS  a known name in an unknown workspace is POSSIBLE, not PROVEN  <-- pinned defect
PASS  a call site with no resolvable name might be this dataset  <-- pinned defect
...
PASS  a name assigned in both arms of an if is POSSIBLE, never PROVEN  <-- pinned defect
...
PASS  a path bound through a parameter is POSSIBLE, because a caller outside the scanned files may pass another  <-- pinned defect
...
PASS  a parameter hides a module constant of the same name  <-- pinned defect
...
PASS  only the file that might not write the dataset is read only  <-- pinned defect
...
PASS  a destroyer that is only possible exits 3, apart from 1  <-- pinned defect
...
PASS  a unique prefix of --destructive-only is refused by the parser  <-- pinned defect
PASS  a prefix of --rules is refused, and no write flag exists to abbreviate  <-- pinned defect
...
PASS  a file the reader refuses is an error, never a clean answer
PASS  a null byte in the source is an error, not a traceback
PASS  a file that nests deeper than the parser is an error, not a traceback  <-- pinned defect
PASS  a byte order mark does not break the parse  <-- pinned defect
PASS  main reports 2 when one file in the directory is broken
PASS  main reports 1 when it names a destroyer
PASS  main reports 0 when only readers touch the dataset
...
PASS  main --rules reports 3 when the only owner is possible
...
PASS  the self-test leaves no directory behind
...
PASS  check() and raises() really do record a failure  <-- pinned defect
--------------------------------------------------------------------
179 assertions, 0 failed
```

## What already exists

- **grep and ripgrep.** They find every file that mentions a string, in any language and in any
  file type, and they are fast and need nothing installed. That is the right first step, and it is
  the only step for a batch file, a scheduled-task argument or a SQL script, none of which this
  tool reads. They cannot tell a read from a truncate, and they cannot find the call site that
  never spells the name.
- **Editor tracking.** An enterprise geodatabase can record the account that last edited each row
  ([Enable editor tracking](https://pro.arcgis.com/en/pro-app/latest/help/data/geodatabases/overview/enable-editor-tracking.htm)).
  It names a database account, not a script, and a truncate removes the rows that held the name.
  That second point is an inference from the tool description, which says the tool removes all
  rows. It was not tested here, because the tool needs a database.

A GitHub search on 2026-10-22 for this name, and for arcpy, truncate and lineage keywords, found no
repository that does the same job. The two repositories with a similar name are unrelated: one
has an empty description and HTML as its language, the other is JavaScript.

## Requirements

Python 3. Nothing else. The tool reads source code with `ast` from the standard library, so it
never imports `arcpy`, never opens a geodatabase and never reaches the network. You can run it on
a laptop that has no ArcGIS licence and no connection to the database you are asking about.

```
git clone https://github.com/uhsear/whowrites.git
```

The self-test reports 179 assertions, and the count is the same on every interpreter it was run
on: Python 3.9.25, 3.10.20, 3.12 and 3.13.2 on Windows, and Python 3.12.3 on Linux. The source
uses no f-strings and no syntax added after Python 3.8. Branch coverage is 100 percent (913
statements, 302 branches), measured on Windows with
`python -m coverage run --branch whowrites.py --self-test`. The Linux host has no `coverage`
module, so coverage was not measured there. No line is excluded from the measurement.

## Quick start

```
python whowrites.py --self-test
python whowrites.py scripts/ --name Hydrants
```

## Usage

The four scripts below are synthetic. Save them in a folder called `demo` to reproduce every
output in this file.

```python
# demo/nightly_refresh.py
import os
import arcpy

SDE = r"D:\gis\utility.sde"
HYDRANTS = os.path.join(SDE, r"WATERWORKS.PUBLIC.Assets\WATERWORKS.PUBLIC.Hydrants")
VENDOR = r"D:\gis\vendor.gdb\Hydrants"
STAGE = r"D:\gis\stage.gdb\HydrantsStage"


def refresh():
    arcpy.management.CopyFeatures(VENDOR, STAGE)
    arcpy.management.GetCount(HYDRANTS)
    arcpy.TruncateTable_management(HYDRANTS)
    arcpy.Append_management(STAGE, HYDRANTS, "NO_TEST")


refresh()
```

```python
# demo/change_report.py
import arcpy

TODAY = r"D:\gis\utility.sde\WATERWORKS.PUBLIC.Assets\WATERWORKS.PUBLIC.Hydrants"
CHANGES = r"D:\gis\utility.sde\WATERWORKS.PUBLIC.Assets\WATERWORKS.PUBLIC.HydrantsChanges"

arcpy.TruncateTable_management(CHANGES)
with arcpy.da.SearchCursor(TODAY, ["OBJECTID"]) as cursor:
    pass
```

```python
# demo/purge_scratch.py
import arcpy


def purge(name):
    arcpy.management.DeleteFeatures(r"D:\gis\scratch.gdb" + "\\" + name)
```

```python
# demo/sync_vendor.py
import os
import arcpy

if arcpy.Exists(r"D:\gis\utility.sde"):
    TARGET = r"D:\gis\utility.sde\WATERWORKS.PUBLIC.Assets\WATERWORKS.PUBLIC.Hydrants"
else:
    TARGET = r"D:\gis\fallback.gdb\Hydrants"
arcpy.management.DeleteFeatures(TARGET)
arcpy.management.Append(r"D:\gis\vendor.gdb\Hydrants",
                        os.path.join(arcpy.env.workspace, "Hydrants"), "NO_TEST")
```

`grep -l Hydrants demo/*.py` returns `change_report.py`, `nightly_refresh.py` and
`sync_vendor.py`. It does not return `purge_scratch.py`, which never contains the word. This is the
tool's answer for the same folder:

```
$ python whowrites.py demo --name Hydrants
whowrites: 4 file(s), 13 call site(s)

Hydrants
  EMPTY  demo\sync_vendor.py:8  DeleteFeatures(in_features)
         D:\gis\fallback.gdb\Hydrants
  WRITE  demo\sync_vendor.py:9  Append(target)
         ?\Hydrants
  READ   demo\nightly_refresh.py:11  CopyFeatures(in_features)
         D:\gis\vendor.gdb\Hydrants
  READ   demo\sync_vendor.py:9  Append(inputs)
         D:\gis\vendor.gdb\Hydrants

WATERWORKS.PUBLIC.Hydrants
  EMPTY  demo\nightly_refresh.py:13  TruncateTable_management(in_table)
         D:\gis\utility.sde\WATERWORKS.PUBLIC.Assets\WATERWORKS.PUBLIC.Hydrants
  WRITE  demo\nightly_refresh.py:14  Append_management(target)
         D:\gis\utility.sde\WATERWORKS.PUBLIC.Assets\WATERWORKS.PUBLIC.Hydrants
  READ   demo\change_report.py:7  SearchCursor(in_table)
         D:\gis\utility.sde\WATERWORKS.PUBLIC.Assets\WATERWORKS.PUBLIC.Hydrants
  READ   demo\nightly_refresh.py:12  GetCount(in_rows)
         D:\gis\utility.sde\WATERWORKS.PUBLIC.Assets\WATERWORKS.PUBLIC.Hydrants

DESTROYED BY:
  demo\nightly_refresh.py:13  TruncateTable_management  (EMPTY)
  demo\sync_vendor.py:8  DeleteFeatures  (EMPTY)
READ ONLY: demo\change_report.py

1 destructive call site(s) whose target could not be resolved. The answer above is incomplete:
  EMPTY  demo\purge_scratch.py:5  DeleteFeatures(in_features)
         D:\gis\scratch.gdb\?
```

`change_report.py` calls `TruncateTable` and is still listed as a reader, because what it truncates
is `WATERWORKS.PUBLIC.HydrantsChanges`. The two groups are two different datasets that both answer
to the name `Hydrants`: the published feature class and a vendor delivery in a file geodatabase.

The last block is the tool telling you where it is blind. `purge_scratch.py:5` empties something,
and the name is assembled from a parameter of a function nobody calls, so no name exists to report.

Look at `sync_vendor.py:8` in that report. It is listed as `EMPTY` on `D:\gis\fallback.gdb\Hydrants`,
and the only reason is that the `else` arm is the one the walk saw last. If the `if` arm ran, the
call emptied the published layer. The default report cannot say which. That is what `--rules` is for.

| Flag | Default | What it does |
|---|---|---|
| `PATHS` | none | Python files or directories to read. Required. |
| `--name` | none | Report only the call sites against this dataset name. Owner qualified or not, case insensitive. |
| `--destructive-only` | off | Report only the call sites that empty or drop a dataset. |
| `--rules` | off | Grade each call site that may own `--name` as PROVEN or POSSIBLE, and report the two apart. Needs `--name`. |
| `--self-test` | off | Run the offline assertions and exit. |

There is no `--apply`, because there is nothing to apply. The tool reads files and prints. The
parser is built with `allow_abbrev=False`, so a prefix of a flag is refused instead of guessed
([argparse](https://docs.python.org/3/library/argparse.html) accepts unique prefixes by default).
The self-test pins that for `--destructive-only` and `--rules`, and pins that `--apply` is not a flag.

## Proven and possible ownership

`--rules --name Hydrants` answers a narrower question than the default report: which call sites
own this dataset, and how sure is the tool? A call site owns a dataset when it writes, empties or
drops it. A reader is never graded.

```
$ python whowrites.py demo --name Hydrants --rules
whowrites: 4 file(s), 13 call site(s)

OWNERSHIP OF Hydrants

PROVEN (path resolved in full from literals in the file): 2 call site(s)
  EMPTY  demo\nightly_refresh.py:13  TruncateTable_management(in_table)
         D:\gis\utility.sde\WATERWORKS.PUBLIC.Assets\WATERWORKS.PUBLIC.Hydrants
  WRITE  demo\nightly_refresh.py:14  Append_management(target)
         D:\gis\utility.sde\WATERWORKS.PUBLIC.Assets\WATERWORKS.PUBLIC.Hydrants

POSSIBLE (not ruled out, and not proven): 3 call site(s)
  EMPTY  demo\purge_scratch.py:5  DeleteFeatures(in_features)
         D:\gis\scratch.gdb\?
         why possible: the target name could not be resolved, so it could be this dataset
  EMPTY  demo\sync_vendor.py:8  DeleteFeatures(in_features)
         D:\gis\fallback.gdb\Hydrants
         why possible: a name was assigned inside a branch, so another branch may give it another value
  WRITE  demo\sync_vendor.py:9  Append(target)
         ?\Hydrants
         why possible: the workspace is unknown, so a dataset of this name in another workspace could be the target

READ ONLY: demo\change_report.py
```

A call site is graded `PROVEN` only when all of these hold:

1. The call site is not a read.
2. The path resolved from literals in the file, with no unknown piece.
3. The last segment answers to the name, by the same whole-segment match as the default report.
4. No name in the path got its value inside a branch, or through a parameter.

Anything else that could still be the dataset is `POSSIBLE`, and each possible call site prints
why. There are four reasons:

| Reason | When |
|---|---|
| target name unknown | The path has no resolvable last segment. The call site is listed whatever name you ask about, because it might be the one. |
| workspace unknown | The name matches, but part of the path before it is `?`, so a dataset of that name in another workspace could be the target. |
| branch | A name was assigned inside an `if` arm, a loop body or an `except` handler. The walk keeps the last value it saw, which may not be the one that ran. |
| parameter | The path reached the call through a function parameter. Every call in the scanned files passes it, but a caller in another file could pass something else. |

The reasons travel. A name built from a doubtful name by `+`, by an f-string or by `os.path.join`
is doubtful. A layer made from a doubtful path passes the doubt to the `DeleteFeatures` that uses
the layer. An assignment that always runs afterwards clears it. `try`, `finally` and `with` bodies
run on the normal path, so they do not count as branches.

`READ ONLY` lists a file only when it has no call site that is proven or possible. In the output
above, `change_report.py` is the only one. A file with an unresolved write is never listed as a
reader, even when it also reads the dataset.

`--destructive-only` works with `--rules`. It hides the proven and possible `WRITE` sites, and it
does not turn a file with a hidden writer into a reader.

Run on only the two scripts that cannot be proven, the tool exits 3 (checked with `echo $?`):

```
$ python whowrites.py demo/sync_vendor.py demo/purge_scratch.py --name Hydrants --rules
whowrites: 2 file(s), 5 call site(s)

OWNERSHIP OF Hydrants

PROVEN (path resolved in full from literals in the file): 0 call site(s)

POSSIBLE (not ruled out, and not proven): 3 call site(s)
  EMPTY  demo/purge_scratch.py:5  DeleteFeatures(in_features)
         D:\gis\scratch.gdb\?
         why possible: the target name could not be resolved, so it could be this dataset
  EMPTY  demo/sync_vendor.py:8  DeleteFeatures(in_features)
         D:\gis\fallback.gdb\Hydrants
         why possible: a name was assigned inside a branch, so another branch may give it another value
  WRITE  demo/sync_vendor.py:9  Append(target)
         ?\Hydrants
         why possible: the workspace is unknown, so a dataset of this name in another workspace could be the target
```

This release also fixed one defect in both modes. A function parameter named like a module
constant used to resolve to the constant. `def wipe(P)` beside a module-level `P = ".../Hydrants"`
was reported as truncating `Hydrants`, when the caller chose the path. The parameter now hides the
constant, and the self-test pins it.

## What it checks

Every call to one of 19 arcpy entry points gets one of four verdicts, per argument:

| Verdict | Meaning | Where it comes from |
|---|---|---|
| `DROP` | The dataset object is removed | `Delete` |
| `EMPTY` | Every row is removed, the object stays | `TruncateTable`, `DeleteFeatures`, `DeleteRows` |
| `WRITE` | Rows or fields are added or changed | `Append` target, `CopyFeatures` output, `InsertCursor`, `UpdateCursor`, `CalculateField`, `AddField`, `DeleteField`, `CopyRows` output |
| `READ` | Nothing is changed | `SearchCursor`, `GetCount`, `Exists`, `MakeFeatureLayer`, `MakeTableView`, `SelectLayerByAttribute`, `Append` inputs, `CopyFeatures` input, `CopyRows` input, `FeatureClassToFeatureClass` input |

`DROP` is separate from `EMPTY` on purpose. `arcpy.Delete_management(fc, "FeatureClass")` removes
the feature class itself, so every map, service and script that referenced it breaks. A truncate
leaves an empty feature class, and the map still draws. The remedies are not the same, so the
verdicts are not either.

To decide which dataset a call site acts on, the tool resolves the path:

- A module constant read inside a function, however far below it.
- `os.path.join`, including its rule that an absolute segment discards everything before it.
- String concatenation with `+`, and `+=`.
- f-strings whose pieces all resolve.
- `os.sep` and `os.path.sep`.
- A constant passed into a function, when every call site passes the same one. The value follows
  up to three calls, so `main()` to `process()` to `replace()` resolves.
- A layer name from `MakeFeatureLayer` or `MakeTableView`, resolved back to the feature class
  underneath it. `DeleteFeatures("hyd_lyr")` is reported against the feature class, and the report
  still says which layer the line was written against.

What it cannot resolve, it says. A path with an unknown piece prints with a `?` in place of that
piece. A destructive call site whose dataset name is unknown is listed at the end of every report,
under a line that says the answer is incomplete.

Name matching is anchored on whole dot-separated segments. `WATERWORKS.PUBLIC.Hydrants` answers to
`Hydrants`, to `PUBLIC.Hydrants` and to its full name. It does not answer to `Hydrant`, and
`HydrantsChanges` does not answer to `Hydrants`. Both directions of that mistake have a pinned
assertion in the self-test, because both of them lose a morning.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | The report named no destructive call site. |
| 1 | The report named at least one call site that empties or drops a dataset. |
| 2 | A file could not be read or parsed, so the answer is incomplete. |
| 3 | `--rules` only. No destructive call site is proven, and at least one is possible. |
| 64 | A usage error: no path, a path that does not exist, or `--rules` without `--name`. |

In `--rules` mode, 1 means a destructive call site is proven and 3 means none is proven but one is
possible. A possible call site that only writes rows does not change the exit code. Without
`--rules`, the exit code is as before and 3 is never returned.

Exit 2 wins over exit 1 and 3. A run that could not read every file gives you an answer that may be
missing the call site you are looking for, and that matters more than what it did find.

## Limits

- It reports what the source says, not what ran. A script nobody schedules is reported the same as
  the nightly job, and a scheduled task pointing at a copy on somebody's desktop is invisible.
- Resolution stops at the file. An import from a sibling module is not followed, so a path defined
  in `settings.py` and used in `load.py` does not resolve.
- Parameter binding follows three calls, set by `MAX_BINDING_PASSES`. A longer chain, or a path
  held on `self`, is reported as unresolved rather than guessed at. An unknown workspace joined to
  a known dataset name still resolves the name, which is what a `--name` query needs.
- Branches are not merged. A name assigned in both arms of an `if` resolves to whichever assignment
  the walk saw last, and the default report prints that guess as a fact. The self-test pins that
  behaviour rather than hiding it. `--rules` grades such a call site `POSSIBLE`. Two arms that
  assign the same value are graded `POSSIBLE` too, because the tool does not compare arms. The
  `if` condition is never evaluated.
- `PROVEN` means the source proves the path. It does not mean the script ran, or that it runs
  tonight. It is also not a claim about one workspace: if the same name is written in two
  resolved workspaces, both call sites are `PROVEN`, and the path under each one tells them apart.
- `%` formatting and `str.format` are not resolved. `os.path.join` and `+` are.
- A constant in a class body is not resolved. Module-level constants are, inside every function in
  the file.
- A tool outside the table of 19 is not classified at all. That is the safe direction, and it means
  a destructive tool this does not know about is silently absent. Add it to `TOOLS` at the top of
  the file.
- A selection is not modelled. Per the Esri tool pages, `DeleteFeatures` and `DeleteRows` against a
  layer with a selection remove the selected rows only, and this reports the whole feature class as
  emptied. `TruncateTable` ignores a selection and removes every row, so for that tool the report
  is exact.
- A dataset name is not unique. Two feature classes called `Hydrants` in two workspaces are two
  groups in the report, told apart by the full path printed under each call site.
- File names in the report use the host's separator, so Linux prints `demo/sync_vendor.py` where
  Windows prints `demo\sync_vendor.py`. The counts and lines are otherwise identical (checked on both).
- Resolved paths print with a backslash separator whatever host you run on, because arcpy paths are
  Windows paths. Matching itself accepts either separator.
- `.py`, `.pyt` and `.txt` are read when a directory is walked. A file named directly on the command
  line is read whatever it is called.

## Related

Other single-file tools in this portfolio that pair with this one:

- [safe-republish](https://github.com/uhsear/safe-republish) - refuses the bad truncate at the moment it runs, once you know which script to guard
- [pytlint](https://github.com/uhsear/pytlint) - the same `ast` reading, judging a toolbox's own shape instead of what its calls act on
- [whobreaks](https://github.com/uhsear/whobreaks) - which portal item references another item, the same question one layer up
- [svcsource](https://github.com/uhsear/svcsource) - which service reads which database

## Sources checked

Read on 2026-10-22. Each behaviour this tool depends on, and the page that states it.

| Behaviour | Source |
|---|---|
| Truncate Table removes all rows, ignores a selection, and cannot be recovered | [Truncate Table](https://pro.arcgis.com/en/pro-app/latest/tool-reference/data-management/truncatetable.html) |
| Delete Features removes all features, or only the selected ones when a layer has a selection | [Delete Features](https://pro.arcgis.com/en/pro-app/latest/tool-reference/data-management/delete-features.htm) |
| Delete Rows removes all rows, or only the selected ones | [Delete Rows](https://pro.arcgis.com/en/pro-app/latest/tool-reference/data-management/delete-rows.htm) |
| Delete permanently deletes the data object | [Delete](https://pro.arcgis.com/en/pro-app/latest/tool-reference/data-management/delete.htm) |
| `os.path.join` discards earlier segments when one is absolute | [os.path](https://docs.python.org/3/library/os.path.html) |
| `argparse` accepts a unique prefix of a long option unless `allow_abbrev=False` | [argparse](https://docs.python.org/3/library/argparse.html) |
| Editor tracking records who edited a row | [Enable editor tracking](https://pro.arcgis.com/en/pro-app/latest/help/data/geodatabases/overview/enable-editor-tracking.htm) |

## Contributing

Open an issue or pull request on GitHub.

## Author

Built by [Asir Khan](https://www.linkedin.com/in/asir-khan-310317264/).

## License

MIT.
