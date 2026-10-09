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
PASS  DeleteFeatures empties the rows and leaves the object
PASS  a script that reads Hydrants and truncates HydrantsChanges is a READER of Hydrants  <-- pinned defect
PASS  the same script is still the destroyer of HydrantsChanges  <-- pinned defect
PASS  HydrantsChanges never answers to the name Hydrants
PASS  Hydrants never answers to the name HydrantsChanges
...
PASS  an unknown prefix glued to a name resolves to no name at all  <-- pinned defect
PASS  the last assignment before the call is the one the call sees
PASS  a constant passed into a method reaches the DeleteFeatures inside it
PASS  a path carried through two calls still reaches the delete  <-- pinned defect
PASS  a chain longer than MAX_BINDING_PASSES stays unresolved, not guessed
PASS  a parameter passed two different paths binds to neither
PASS  a function nobody calls has no parameter binding
PASS  deleting from a layer is reported against the feature class  <-- pinned defect
PASS  one layer name built from two datasets resolves to neither
PASS  a local helper named delete is not arcpy.Delete
...
PASS  the verdict names the file and line that truncates Hydrants
PASS  the reader is not listed under DESTROYED BY  <-- pinned defect
PASS  the reader is listed as a reader
PASS  a file that both reads and truncates Hydrants is kept out of the read only list  <-- pinned defect
PASS  a query for Hydrants does not report HydrantsChanges at all
PASS  an unresolved destructive call site is always reported as a caveat
PASS  an invalid escape sequence in the scanned file raises no warning out of this tool  <-- pinned defect
PASS  a unique prefix of --destructive-only is refused by the parser  <-- pinned defect
...
PASS  a file the reader refuses is an error, never a clean answer
PASS  a null byte in the source is an error, not a traceback
PASS  a file that nests deeper than the parser is an error, not a traceback  <-- pinned defect
PASS  a byte order mark does not break the parse  <-- pinned defect
PASS  main reports 2 when one file in the directory is broken
PASS  main reports 1 when it names a destroyer
PASS  main reports 0 when only readers touch the dataset
PASS  the self-test leaves no directory behind
PASS  check() and raises() really do record a failure  <-- pinned defect
--------------------------------------------------------------------
126 assertions, 0 failed
```

## Requirements

Python 3. Nothing else. The tool reads source code with `ast` from the standard library, so it
never imports `arcpy`, never opens a geodatabase and never reaches the network. You can run it on
a laptop that has no ArcGIS licence and no connection to the database you are asking about.

```
git clone https://github.com/uhsear/whowrites.git
```

The self-test was run on Windows with Python 3.13.2 and reports 126 assertions.
The source uses no f-strings and no syntax added after Python 3.8. Only Python 3.13.2 was run
for this count.

## Quick start

```
python whowrites.py --self-test
python whowrites.py scripts/ --name Hydrants
```

## Usage

```
$ python whowrites.py demo --name Hydrants
whowrites: 4 file(s), 14 call site(s)

WATERWORKS.PUBLIC.Hydrants
  EMPTY  demo\nightly_refresh.py:19  TruncateTable_management(in_table)
         D:\gis\utility.sde\WATERWORKS.PUBLIC.Assets\WATERWORKS.PUBLIC.Hydrants
  WRITE  demo\nightly_refresh.py:20  Append_management(target)
         D:\gis\utility.sde\WATERWORKS.PUBLIC.Assets\WATERWORKS.PUBLIC.Hydrants
  READ   demo\change_report.py:11  SearchCursor(in_table)
         D:\gis\utility.sde\WATERWORKS.PUBLIC.Assets\WATERWORKS.PUBLIC.Hydrants
  READ   demo\nightly_refresh.py:18  GetCount(in_rows)
         D:\gis\utility.sde\WATERWORKS.PUBLIC.Assets\WATERWORKS.PUBLIC.Hydrants

Hydrants
  READ   demo\nightly_refresh.py:13  CopyFeatures(in_features)
         D:\gis\vendor.gdb\Hydrants

DESTROYED BY:
  demo\nightly_refresh.py:19  TruncateTable_management  (EMPTY)
READ ONLY: demo\change_report.py

1 destructive call site(s) whose target could not be resolved. The answer above is incomplete:
  EMPTY  demo\purge_scratch.py:12  DeleteFeatures(in_features)
         D:\gis\scratch.gdb\?
```

That answer took four synthetic scripts. `change_report.py` calls `TruncateTable` and is still
listed as a reader, because what it truncates is `WATERWORKS.PUBLIC.HydrantsChanges`. The two
groups above it are two different datasets that both answer to the name `Hydrants`: the published
feature class and a vendor delivery in a file geodatabase.

The last block is the tool telling you where it is blind. `purge_scratch.py:12` empties something,
and the name is assembled from a parameter of a function nobody calls, so no name exists to report.

| Flag | Default | What it does |
|---|---|---|
| `PATHS` | none | Python files or directories to read. Required. |
| `--name` | none | Report only the call sites against this dataset name. Owner qualified or not, case insensitive. |
| `--destructive-only` | off | Report only the call sites that empty or drop a dataset. |
| `--self-test` | off | Run the offline assertions and exit. |

There is no `--apply`, because there is nothing to apply. The tool reads files and prints.

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
| 64 | A usage error: no path, or a path that does not exist. |

Exit 2 wins over exit 1. A run that could not read every file gives you an answer that may be
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
  the walk saw last. The self-test pins that behaviour rather than hiding it.
- `%` formatting and `str.format` are not resolved. `os.path.join` and `+` are.
- A constant in a class body is not resolved. Module-level constants are, inside every function in
  the file.
- A tool outside the table of 19 is not classified at all. That is the safe direction, and it means
  a destructive tool this does not know about is silently absent. Add it to `TOOLS` at the top of
  the file.
- A selection is not modelled. `DeleteFeatures` against a layer with a selection on it removes the
  selected rows only, and this reports the whole feature class as emptied.
- A dataset name is not unique. Two feature classes called `Hydrants` in two workspaces are two
  groups in the report, told apart by the full path printed under each call site.
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

## Contributing

Open an issue or pull request on GitHub.

## Author

Built by [Asir Khan](https://www.linkedin.com/in/asir-khan-310317264/).

## License

MIT.
