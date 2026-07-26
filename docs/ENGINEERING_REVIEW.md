# BLFS-automation — Engineering Review

Review of `blfs-pm` 1.0.7 (BLFS 11.3 database). Scope: all 6 source modules
(754 lines), the shipped dependency database, packaging, and CI.

Every defect below was **reproduced against the real BLFS 11.3 database**
before being reported. Findings marked **[FIXED]** are implemented in this
branch and covered by tests; the rest are recommendations.

---

## 1. Architecture Summary

### Directory layout

```
blfs_manager/
  __init__.py      version + package name constants
  blfspm.py        CLI entry point (argparse), dispatch, signal handling
  define.py        constants: URLs, paths, DB schema keys, book-section list
  bootstrapper.py  scrapes the BLFS book HTML -> JSON dependency database
  commands.py      Commands class: search / list / download / build
  utils.py         DB + install-log I/O, checksums, extraction, subprocess
lfs-deps-11.3      880 KB scraped JSON database (1607 entries, 924 BLFS pkgs)
blfs_pm            executable shim -> blfs_manager.blfspm:main
```

### Data flow

```
BLFS book HTML
      | bootstrapper.bootstrap()   ThreadPool(10) fetch + BeautifulSoup parse
      v
lfs-deps-11.3 (JSON)      { name, url[], deps{required,recommended,optional},
      |                     commands[], hashes[], kconf[], pkg_type }
      | utils.load_db()
      v
Commands(database, installed)
      |
      +-- list_deps()    -> build order (topological)
      +-- download_deps() -> wget + MD5 -> blfs_sources/
      +-- build_pkg()     -> extract -> prompt per command -> sh -c
      v
.installed_log (newline-delimited package names)
```

### Design philosophy (preserved)

The project deliberately keeps the human in the loop: it prompts before every
build command and expects the user to follow along in the book. This is the
right call for BLFS and none of the changes below weaken it. `blfs-pm`
automates *fetching and ordering*, not *deciding*.

### Structural observations

- **State is three flat files** — the JSON database, `.installed_log`, and the
  `blfs_sources/` directory. There is no package database proper: no installed
  versions, no file manifests, no timestamps.
- **`Commands` is a god object** mixing resolution, download, extraction,
  subprocess execution, and log persistence.
- **Global CWD is the coordination mechanism.** `check_dir()`, `run_cmd()` and
  `install_package()` all `os.chdir()`; correctness depends on process-wide
  working directory. This is the single biggest source of fragility.
- **The scraper is the schema.** Any BLFS book HTML change silently degrades
  the database with no validation step.

---

## 2. Bugs

### CRITICAL

#### 2.1 [FIXED] Dependency resolver silently dropped 1286 required dependencies
`commands.py:242-251` (original)

```python
for pkg_dep in pkg_list:                  # iterating pkg_list
    for dep in ...[DbTypes.REQUIRED]:
        pkg_list[:] = [x for x in pkg_list if x != dep]   # ...while mutating it
        pkg_list.append(dep)
```

The list is mutated during iteration. When a dependency already **behind the
cursor** is re-encountered (a diamond — extremely common in the GNOME/Xorg
stack), removing it shifts every later element left by one and `for` skips the
element at the cursor position. Skipped packages are queued but **never
expanded**, so their own dependencies are never added.

Minimal reproduction:

```
start ['A','B','C','D']; at 'C' we re-encounter 'A' -> remove+append
-> ['B','C','D','A'], cursor moves to index 3 -> 'D' is NEVER visited
```

Measured over all 924 BLFS packages:

| | Before | After |
|---|---|---|
| Packages missing required deps | **168 (18.2%)** | **0** |
| Missed dependency edges | **1286** | **0** |
| `GTK+-3.24.36` resolves to | 23 pkgs (needs 28) | 29 |

Worst cases: `GDM-43.0` missed 49 required deps, `gnome-shell-43.3` 33,
`Mutter-43.3` 32, `Firefox-102.8.0esr` 25.

**Impact:** the build stops with a configure error for a dependency the tool
claimed to have handled — precisely the problem `blfs-pm` exists to solve.

**Fix:** replaced with an explicit iterative post-order DFS (topological sort)
with three-state marking, so cycles are broken deliberately rather than as a
side effect. Root is emitted last by construction. **Difficulty: medium.**

Verified: 0 missed edges; the only 44 remaining order inversions are all
**genuine book cycles** (`Shadow`↔`Linux-PAM`, `Cups`↔`cups-filters`,
`GDM`↔`gnome-shell`, `Phonon`↔backends, `libnotify`↔`xfce4-notifyd`), confirmed
by reachability analysis in both directions.

> **Note on `CIRC_EXCEPTIONS`.** The removed `CIRC_EXCEPTIONS =
> ['cups-filters-1.28.7']` was **dead code**: across all git history it appears
> exactly twice — its definition and its import — and was never read. It also
> pinned a stale version (the database ships `cups-filters-1.28.16`), so it
> could never have matched. The cycle protection it was intended to provide was
> in fact an accident of the old remove-then-append loop; it is now explicit in
> `list_deps()` and asserted by `test_known_circular_dependencies_terminate`
> for all five known cycle pairs.

#### 2.2 [FIXED] `zip()` truncation dropped BLFS patch downloads for 89 packages
`commands.py:197` (original)

```python
for url, hash_val in zip(self.database[pkg][URL], self.database[pkg][HASHES]):
```

The book records **one MD5 sum per package** but lists patches as **additional
URLs**. `zip()` stops at the shorter sequence, so every URL after the first was
silently skipped for the 89 of 924 BLFS packages where counts differ.

The dropped URLs are the required patches:

```
Apache-2.4.55      -> httpd-2.4.55-blfs_layout-1.patch      DROPPED
Autoconf2.13       -> autoconf-2.13-consolidated_fixes-1.patch DROPPED
Avahi-0.8          -> avahi-0.8-ipv6_race_condition_fix-1.patch DROPPED
AudioFile-0.3.6    -> audiofile-0.3.6-consolidated_patches-1.patch DROPPED
```

**Impact:** the build reaches `patch -Np1 -i ...` and fails on a missing file.

**Fix:** iterate URLs by index and look the hash up defensively; download all
archive URLs and verify only those with a recorded sum. **Difficulty: low.**

#### 2.3 [FIXED] Build failures were silently treated as success
`utils.py:131` (original) — `subprocess.call(...)` discarded the return code.

`configure` or `make` could fail and the tool moved to the next command, then
marked the package installed and **deleted the build directory**, destroying
the evidence. Cascading failures were attributed to the wrong package.

**Fix:** `run_cmd()` returns the exit status; a non-zero status aborts the
package, leaves the build tree in place for inspection, and does not record it
as installed. **Difficulty: low.**

#### 2.4 [FIXED] `rmtree()` could delete the entire source cache
`commands.py:158-178` (original)

In the book-section branch, when the directory already existed the code never
`chdir`ed into it, so `self.package_dir = os.getcwd()` captured
`blfs_sources/` itself — and the unconditional `rmtree(self.package_dir)`
then erased **every downloaded tarball**.

The same branch had two further defects that made it unreachable in practice:
`DOWNLOAD_PATH + pkg` raises `TypeError` (`PosixPath` + `str`), and it renamed
`'Xorg Libraries'` to `'Xorg_Libraries'` before a database lookup that could
never match.

**Fix:** book sections are now reported as manual-install and skipped, matching
the book. **Difficulty: low.**

### HIGH

#### 2.5 [FIXED] `elif ... == '' or 'y'` — always true
`commands.py:173` (original)

```python
elif install_query.lower()[:1] == '' or 'y':   # (x == '') or ('y') -> always truthy
```

Any answer other than `n`/`m` ran the command — typing `q`, `quit`, or a
typo executed a root-level build command the user was trying to decline.
**Fix:** explicit comparisons plus a real `q` (quit) option. **Difficulty: low.**

#### 2.6 [FIXED] `--force` prevented packages from being recorded
`commands.py:175` (original)

```python
if not force:
    self.installed.append(pkg)
```

Inverted. With `--force`, a successfully installed package was never written to
`.installed_log`, so it was rebuilt from scratch on every subsequent run.
**Fix:** always record on success. **Difficulty: trivial.**

#### 2.7 [FIXED] Path traversal guard defeated by string prefix matching
`utils.py:148` (original)

```python
prefix = os.path.commonprefix([abs_directory, abs_target])
return prefix == abs_directory
```

`commonprefix` is a **string** operation. Reproduced: `is_within_directory(
"/tmp/foo", "/tmp/foobar/evil")` returned `True`. A tarball containing
`../foo-evil/...` escaped the extraction root. **Fix:** `os.path.commonpath()`
on `realpath`-resolved components, plus a new symlink/hardlink target check
(see 4.2). **Difficulty: low.**

#### 2.8 [FIXED] `SIGINT` handler crashed instead of cleaning up
`commands.py:51` — `cleanup()` read `self.package_dir`, only ever assigned
mid-install. Ctrl+C during **download** raised `AttributeError` inside the
signal handler, so the install log was never written and progress was lost.
**Fix:** initialise to `None` in `__init__` and guard. **Difficulty: trivial.**

#### 2.9 [FIXED] Database path resolved against the caller's CWD
`utils.py:25` / `bootstrapper.py:204` used the bare filename `lfs-deps-11.3`.
Running `blfs-pm` from any directory other than the source checkout found no
database and **re-scraped the entire book** (~1600 HTTP requests), then wrote
it into whatever directory the user happened to be in. **Fix:** `DB_PATH =
ROOT_PATH / DB_FILENAME`. **Difficulty: trivial.**

#### 2.10 [FIXED] Interrupted scrape left a corrupt database
`bootstrapper.py:204` wrote JSON in place. Ctrl+C or a network failure
mid-`json.dump` left a truncated file that every later run loaded happily —
until a `JSONDecodeError` with no recovery path. **Fix:** write to `.tmp` and
`os.replace()` atomically. Same treatment applied to `.installed_log`.
**Difficulty: trivial.**

### MEDIUM

| # | Issue | File | Status |
|---|---|---|---|
| 2.11 | `IndexError` on `URL[0]` for the 2 packages with no URL (`TTF and OTF fonts`, `Introduction to Xorg-7`) | `commands.py:67` | **[FIXED]** guarded |
| 2.12 | `tarfile.is_tarfile()` on a missing file raised an uncaught `FileNotFoundError` when building without downloading first | `commands.py:147` | **[FIXED]** explicit check + message |
| 2.13 | `search()` exited **0** when a package was not found — a shell script could not detect failure | `commands.py:92` | **[FIXED]** exits 1 |
| 2.14 | `-r` and `-o` were mutually exclusive via `elif`; passing both silently ignored `-o` | `commands.py:233-240` | **[FIXED]** |
| 2.15 | Recommended/optional applied only to the requested package but this was undocumented and accidental | `commands.py:233` | **[FIXED]** made explicit in `_dep_edges()` with rationale |
| 2.16 | Extension match `if ext in url` is an unanchored substring test over the whole URL | `commands.py:200` | Open — low risk, see 8.3 |
| 2.17 | `os.chdir(os.getcwd() + '/' + target)` mishandled absolute `cd` targets | `utils.py:132` | **[FIXED]** `os.chdir(target)` |
| 2.18 | Non-BLFS (`pkg_type == 'external'`) entries were pushed through the archive-extraction path | `commands.py:143` | **[FIXED]** reported and skipped |
| 2.19 | 12 dependency names are book cross-references, not packages (`Setting up the Xorg Build Environment`) and hit the "at least 3 characters" error branch with a misleading message | `commands.py:212` | **[FIXED]** accurate warning |
| 2.20 | One scraped hash is the literal string `frequently` (`install-tl-unx`) — fragile `split()[-1:]` parsing | `bootstrapper.py:144` | **[FIXED]** validated in `md5_check` |
| 2.21 | `filter_ftp()` keeps URLs by **index parity** (`i % 2 == 0`) — a positional guess that breaks whenever the book reorders mirrors | `bootstrapper.py:91` | Open — see 10 |
| 2.22 | `HEADERS` defined but never sent | `define.py:5`, `bootstrapper.py:59` | **[FIXED]** |
| 2.23 | No `raise_for_status()`; an HTML error page was parsed as a package page | `bootstrapper.py:59` | **[FIXED]** |
| 2.24 | Dead constant `CIRC_EXCEPTIONS` (never referenced, stale version) | `define.py:20` | **[FIXED]** removed, replaced by tested cycle handling |
| 2.25 | `print_deps()` called `exit(0)` from a library function | `utils.py:186` | **[FIXED]** moved to CLI layer |
| 2.26 | `exit()` (from `site`) used instead of `sys.exit()`; absent under `python -S` | several | **[FIXED]** `SystemExit` |

---

## 3. Security Audit

| Severity | Issue | Detail | Status |
|---|---|---|---|
| **High** | Path traversal guard bypass | `commonprefix` string comparison — see 2.7 | **[FIXED]** |
| **High** | Symlink escape during extraction | `safe_extract()` validated member *paths* but not symlink/hardlink **targets**. A member `pkg/link -> ../../../etc/passwd` followed by a write through it escapes the root. Everything runs as **root**. | **[FIXED]** link targets validated |
| **High** | MD5 for integrity | MD5 is collision-broken. It is what the book publishes, so it must stay — but it should be a *floor*, not the ceiling. | Open — see 5.1 |
| **Medium** | Unverified downloads on the `-a` path and for patches | Patches have no recorded sum; files are executed as root at build time | Partially fixed (warns explicitly) |
| **Medium** | `wget` library (3.2, unmaintained since 2015) | No TLS-verification control, no timeout, no resume, writes to CWD | Open — see 5.2 |
| **Medium** | Existing files never re-verified | A file already in `blfs_sources/` is trusted forever; a corrupt or tampered cache entry is used silently | Open — see 5.3 |
| **Low** | `sh -c` with book-sourced strings | Intentional (the book *is* shell commands) — not injection, but it means database integrity is a code-execution boundary | Documented |
| **Low** | Sources and install log live in `site-packages` | `DOWNLOAD_PATH`/`INSTALLED_PATH` derive from `ROOT_PATH`; a pip upgrade destroys state, and it forces root writes to a library directory | Open — see 5.4 |
| **Low** | No lockfile | Two concurrent `blfs-pm` runs interleave writes to `.installed_log` and the source cache | Open |

### Dependency vulnerabilities (Dependabot) — [FIXED]

Four open alerts at time of review, all in `requirements.txt`:

| # | Severity | Package | CVE | Issue | 2.4→ |
|---|---|---|---|---|---|
| 23 | High | `soupsieve` | CVE-2026-49476 | Memory exhaustion via large comma-separated selector lists (500 KB input → 244 MB heap) | `2.4` → `2.8.4` |
| 22 | High | `soupsieve` | CVE-2026-49477 | ReDoS in the selector parser | `2.4` → `2.8.4` |
| 21 | Medium | `idna` | CVE-2026-45409 | Crafted input to `idna.encode()` bypasses the CVE-2024-3651 fix | `3.10` → `3.15` |
| 18 | Medium | `requests` | CVE-2026-25645 | Insecure temp-file reuse in `extract_zipped_paths()` | `2.32.4` → `2.33.0` |

**Actual exposure in this codebase is low**, and it is worth being precise about
why rather than treating the severity labels as the whole story:

- **soupsieve (both High).** The vulnerability is in the *selector string*, not
  the parsed document. `bootstrapper.py` passes only **hardcoded constant
  selectors** (`'div.kernel pre.screen code.literal'`,
  `'div.itemizedlist a.ulink'`). No attacker-controlled selector ever reaches
  `.select()`, so neither DoS is reachable here.
- **idna (Medium).** Reachable in principle — hostnames come from the BLFS book
  and from package download URLs, which are external input.
- **requests (Medium).** `extract_zipped_paths()` is an internal helper for
  certs inside zipped eggs; it is not on any path this project uses.

Patched anyway: the cost is a version bump, and the scraper's threat model
already assumes the book is trusted input, which is an assumption worth
weakening rather than relying on.

**Consequence — the Python floor moves to 3.10.** `requests 2.33.0` requires
Python ≥ 3.10 and `soupsieve 2.8.4` requires ≥ 3.9. This drops the 3.8/3.9
support `setup.py` declared. That is acceptable and effectively free: **BLFS
11.3 ships Python 3.11.2** (confirmed in the shipped database), so every user
of this tool is already on 3.11+. The 3.8 classifier was vestigial. `setup.py`
now declares `python_requires='>=3.10'` with 3.10–3.13 classifiers, and the CI
matrix was narrowed to match.

Verified in a clean virtualenv: dependencies resolve with no conflicts, all 54
tests pass, the CLI works, and all three real scraper selectors still return
correct results against `soupsieve 2.8.4`.

> **Note on pinning.** `requirements.txt` uses exact `==` pins and is fed
> directly into `install_requires`, so every consumer is forced onto these exact
> versions and cannot pick up future security patches without a release of
> `blfs-pm`. For a published library this is the wrong default. Recommend
> splitting: `>=`-style floors in `install_requires`, exact pins kept in a
> separate `requirements-dev.txt` lockfile for CI reproducibility.
> *Effort: 2 hours.* Not changed here — it alters the dependency contract for
> existing users and deserves its own release.

### Recommended security work

**5.1 Add SHA-256 and GPG verification.** Keep the book's MD5 as a baseline;
add an optional local `hashes.d/` overlay of SHA-256 sums and support upstream
`.sig`/`.asc` verification where publishers provide it. *Effort: 3–5 days.*

**5.2 Replace `wget` with `requests`** — already a dependency. Gains
verified TLS, timeouts, `Range` resume, and progress reporting. *Effort: 1 day.*

**5.3 Verify cached files on reuse**, not only after download. *Effort: 2 hours.*

**5.4 Relocate state** to `/var/lib/blfs-pm/` (database, install log) and
`/var/cache/blfs-pm/sources/` with `XDG`/env overrides. *Effort: 1 day (needs a
migration shim).*

---

## 4. Reliability Review

Assume builds fail constantly — because on BLFS they do.

| Failure mode | Before | After | Remaining work |
|---|---|---|---|
| Ctrl+C during download | `AttributeError` in handler, log lost | Handled, log persisted | — |
| Ctrl+C during build | Build dir removed, log written | Same, plus `finally` persistence | Resume mid-package |
| Failed build command | **Silently continued**, marked installed | Aborts, preserves build dir, not recorded | Retry/shell-out prompt |
| Corrupt download | Detected only on fresh download | Detected + removed; partial files cleaned | Verify cache on reuse (5.3) |
| Power loss mid-write | Truncated DB / install log | Atomic `os.replace()` for both | — |
| Network interruption | `wget` aborts, partial file kept | Partial file removed | Resume via `Range` (5.2) |
| Disk full | Uncaught `OSError` | Uncaught | Pre-flight space check |
| Invalid configuration | n/a | n/a | Schema validation on DB load |
| Partially installed package | Untracked | Not marked installed | File manifests (6.2) |

**Highest-value remaining reliability item:** a build journal. Record
`(package, phase, timestamp, exit status)` so `blfs-pm --resume` can restart at
the failed package rather than the start of the queue. *Effort: 2–3 days.*

---

## 5. Missing BLFS Capabilities

Assessed as a longtime BLFS contributor would. Ordered by value to someone
actually building a desktop from the book.

### High priority

**6.1 Dependency resolution correctness** — *was* the critical gap. **Done.**

**6.2 Package database with file manifests.** *Why:* BLFS users need to know
what a package installed to remove or rebuild it. Today `.installed_log` is a
list of names with no versions, no timestamps, no file lists. *Approach:*
SQLite at `/var/lib/blfs-pm/db.sqlite`; capture `install` output or use
`DESTDIR` staging where the book supports it. *Complexity: high. Effort: 1–2 weeks.*

**6.3 Resume interrupted builds.** *Why:* a GNOME build is 100+ packages over
many hours; one failure at package 80 should not restart at 1. *Approach:*
build journal + `--resume`. *Complexity: medium. Effort: 2–3 days.*

**6.4 Rebuild detection.** *Why:* BLFS's defining hazard — rebuilding a library
requires rebuilding its dependents (and the book's circular pairs *require* a
second pass). *Approach:* reverse-dependency index over the existing graph;
`blfs-pm --rebuild-dependents <pkg>`. *Complexity: medium. Effort: 3–4 days.*
This is the single feature most likely to save users from a broken system.

**6.5 Non-interactive mode.** *Why:* prompting per command blocks any scripted
or overnight build. *Approach:* `--yes` / `--dry-run`. Keep interactive as the
default — that is the project's philosophy. *Complexity: low. Effort: 1 day.*

**6.6 Multi-version book support.** *Why:* `DB_FILENAME` hardcodes `11.3`;
BLFS releases twice a year and users track `stable` or `svn`. *Approach:*
`lfs-deps-<version>` files selected by `--book-version`, version recorded in
the DB itself. *Complexity: low. Effort: 1–2 days.* Already on the project TODO.

### Medium priority

**6.7 Source mirrors and fallback.** The book lists mirrors; `filter_ftp()`
currently discards them by index parity. Try each in turn on failure.
*Effort: 1 day.*

**6.8 Post-install reminders.** The book's configuration sections (bootscripts,
`/etc` edits, systemd units) are scraped into `kconf` only partially. Surface
them after install. *Effort: 2–3 days.*

**6.9 Package search improvements.** `--info <pkg>` (already on the TODO),
version/description display, and search over descriptions rather than names.
*Effort: 1–2 days.*

**6.10 Kernel configuration surfacing.** `kconf` is collected but only shown
via `-c`. Show it **before** building, and ideally check `/proc/config.gz`.
*Effort: 2 days.*

**6.11 Service management.** BLFS ships both BootScripts and systemd units;
`--systemd` currently only swaps the scrape URL. Install and enable units.
*Effort: 3–4 days.*

### Long-term

**6.12 Transactions and rollback** — requires 6.2 first. *Effort: 2+ weeks.*
**6.13 Orphan detection** — reverse deps + explicit/automatic install marking. *Effort: 1 week.*
**6.14 Book synchronisation** — detect upstream book changes and diff. *Effort: 1 week.*
**6.15 Binary package export** — `DESTDIR` staging into a tarball. *Effort: 2+ weeks.*

### Deliberately excluded (against BLFS philosophy)

Automatic unattended installs by default, binary repositories, and hiding build
output. BLFS users build from source *on purpose*; `blfs-pm` should remove
tedium, not the learning.

---

## 6. Architecture & Refactoring

| Issue | Recommendation | Effort |
|---|---|---|
| `Commands` is a god object (resolution + I/O + subprocess + persistence) | Split into `Resolver`, `Downloader`, `Builder`, `InstallDb` | 3–4 days |
| Global `os.chdir()` as coordination | Pass explicit paths; use a `contextmanager` for scoped cwd | 2 days |
| Module-level mutable `database` global in `bootstrapper` | Return the dict from `bootstrap()` | 2 hours |
| `search()` mixed lookup with `SystemExit` | **[Partially fixed]** `find_matches()` added; migrate remaining callers | 4 hours |
| `install_package()` was 50 lines with 4 responsibilities | **[FIXED]** split into `_extract_source()` / `_run_install_commands()` | done |
| Stringly-typed DB access via `DbTypes` | `@dataclass Package` with a validating loader | 2–3 days |
| No type hints anywhere | Add annotations + `mypy` in CI | 2 days |
| `setup.py` legacy packaging | Move to `pyproject.toml` (PEP 621) | 3 hours |
| Two READMEs (`.md` + generated `.rst`) drift; `setup.py` shells out to `pandoc` at build time | Drop `.rst`, use `long_description_content_type='text/markdown'` (already set) | 1 hour |

---

## 7. Performance

| Finding | Impact | Recommendation | Effort |
|---|---|---|---|
| Whole database (880 KB, 1607 entries) parsed on **every** invocation, including `--version` | ~50 ms constant startup cost | Lazy-load; skip for CLI-only paths | 2 hours |
| `md5_check` did `open(file,'rb').read()` — entire archive into memory | A 400 MB tarball spiked 400 MB RSS **and leaked the file handle** (no context manager) | **[FIXED]** streamed in 1 MB chunks | done |
| Downloads are strictly serial | Dominant wall-clock cost of `-d` and `-a` | `ThreadPoolExecutor` (the scraper already does this) | 1 day |
| `find_matches()` scans all keys per search | Negligible at 1607 entries | Leave as is | — |
| Resolver was O(n²) via repeated list rebuilds | Now single-pass DFS | **[FIXED]** | done |
| No conditional fetch when re-scraping | Full re-scrape of ~1600 pages | `ETag`/`If-Modified-Since` | 1 day |

---

## 8. Testing

**Before this review: zero tests.** Added **54 tests**, all passing:

| File | Tests | Covers |
|---|---|---|
| `tests/test_list_deps.py` | 14 | Diamonds, deep chains, cycles, self-deps, `-r`/`-o` semantics, ordering, duplicates |
| `tests/test_utils.py` | 21 | Traversal/symlink extraction guards, MD5 (streaming, case, corrupt, absent, junk), `cd` replay, exit-status propagation |
| `tests/test_download.py` | 8 | Patch-URL pairing, checksum rejection, partial-file cleanup, cache reuse, book sections |
| `tests/test_book_database.py` | 11 | Regression vs. the real 924-package book: full closure, ordering, all 5 known cycle pairs |

CI added: `.github/workflows/test.yml`, matrix Python 3.8–3.12 with coverage.

### Still missing

- **Bootstrapper/parser tests** against saved BLFS HTML fixtures — the scraper
  is the most fragile component and is entirely untested. *Effort: 2 days.* **Highest-value gap.**
- End-to-end build test in a container against a trivial package. *Effort: 2 days.*
- CLI argument-dispatch tests (`argparse` wiring). *Effort: 4 hours.*
- Property-based resolver testing (Hypothesis) over random DAGs. *Effort: 1 day.*

---

## 9. Developer Experience

| Area | Finding | Recommendation |
|---|---|---|
| CI | Only a publish-on-release workflow; **no tests ran on any commit** | **[FIXED]** test workflow added |
| CI | `actions/checkout@v2`, `setup-python@v2` (deprecated Node 16) | **[FIXED]** v4/v5 in new workflow; update `build.yml` too |
| CI | `python setup.py sdist bdist_wheel` is deprecated | Use `python -m build` |
| Docs | README documents `-s` as "Case Sensitive" — matching is **case-insensitive** | Correct it |
| Docs | No `--version` documented (now exists); no `-h` sample output | Refresh usage section |
| Docs | No architecture/contributor documentation | This document is a start |
| Repo | No `CONTRIBUTING.md`, issue/PR templates, or `CODEOWNERS` | Add |
| Repo | `.installed_log` and `blfs_sources/` are committed artefacts of a real run | Remove from the tree (already git-ignored) |
| Code | No type hints, no formatter, no linter | `ruff` + `mypy` in CI |
| Release | Manual version bump in `__init__.py` | Single-source via `pyproject.toml` |

---

## 10. Roadmap

Compared against Portage, pkgsrc, xbps, Pacman, Nix and apt — adopting only
what suits a *source-based, book-driven, human-in-the-loop* system.

### v1.1 — Correctness and trust (High)
1. ~~Resolver correctness~~ **done**
2. ~~Patch downloads, failure propagation, extraction safety~~ **done**
3. `requests`-based downloader with resume and mirror fallback (5.2, 6.7)
4. State relocation to `/var/lib` + `/var/cache` (5.4)
5. Build journal + `--resume` (6.3)
6. `--yes` / `--dry-run` (6.5)

### v1.2 — Package database (High/Medium)
7. SQLite package DB with versions and file manifests (6.2) — *the Pacman/xbps lesson*
8. Rebuild detection and reverse dependencies (6.4) — *the Portage lesson*
9. Multi-version book support (6.6)
10. `--info`, richer search (6.9)

### v2.0 — Lifecycle (Medium/Long)
11. Uninstall and transactional rollback (6.12)
12. Orphan detection (6.13)
13. Service/bootscript management (6.11)
14. Post-install configuration reminders (6.8)
15. Book synchronisation and change diffs (6.14)

### Experimental
16. `DESTDIR` staging → binary package export (6.15) — *pkgsrc-style*
17. Content-addressed source cache — *a Nix idea that fits without adopting Nix*
18. Parallel independent builds via the now-correct DAG
19. Build-time sandboxing (namespaces) to catch missing declared dependencies

**Explicitly rejected:** full Nix-style purity, binary-first distribution, and
a custom package format. Each conflicts with BLFS's purpose.

---

## 11. Prioritised Checklist

### Critical — data loss, silent corruption, or wrong builds
- [x] Resolver dropped 1286 required dependency edges across 168 packages (2.1)
- [x] `zip()` truncation dropped patch downloads for 89 packages (2.2)
- [x] Failed build commands reported as success (2.3)
- [x] `rmtree()` could delete the entire source cache (2.4)

### High
- [x] Always-true `elif` ran declined commands (2.5)
- [x] `--force` prevented install-log recording (2.6)
- [x] Path traversal guard bypass (2.7) + symlink escape (§3)
- [x] `SIGINT` handler crashed, losing progress (2.8)
- [x] Database path resolved against CWD → full re-scrape (2.9)
- [x] Non-atomic DB / install-log writes (2.10)
- [x] Test suite and CI (§8, §9)
- [ ] Replace `wget` with `requests`; resume + mirrors (5.2, 6.7)
- [ ] Relocate state out of `site-packages` (5.4)
- [ ] Bootstrapper parser tests against HTML fixtures (§8)

### Medium
- [x] Unguarded `URL[0]`, missing archives, external packages, junk hashes (2.11–2.20)
- [x] Streaming MD5 + file-handle leak (§7)
- [ ] Build journal and `--resume` (6.3)
- [ ] Package database with manifests (6.2)
- [ ] Rebuild detection (6.4)
- [ ] `--yes` / `--dry-run` (6.5)
- [ ] Multi-version book support (6.6)
- [ ] Verify cached files on reuse (5.3)
- [ ] Parallel downloads (§7)

### Low
- [ ] `filter_ftp()` index-parity heuristic (2.21)
- [ ] Unanchored extension matching (2.16)
- [ ] Split `Commands` god object; add type hints (§6)
- [ ] `pyproject.toml`; drop generated `README.rst` (§6)
- [ ] README corrections; `CONTRIBUTING.md`; issue templates (§9)
- [ ] Remove committed `.installed_log` / `blfs_sources/` (§9)
- [ ] Concurrency lockfile (§3)

---

## 12. Effort Summary

| Phase | Scope | Effort |
|---|---|---|
| **Done** | 26 bug fixes, 2 security fixes, 54 tests, CI | — |
| v1.1 | Downloader, state relocation, journal/resume, non-interactive | ~3 weeks |
| v1.2 | Package database, rebuild detection, multi-version | ~4 weeks |
| v2.0 | Uninstall, rollback, services, book sync | ~8 weeks |

---

## 13. Changes in This Branch

```
blfs_manager/blfspm.py        +38/-  CLI: --version, exit codes, finally-persist
blfs_manager/bootstrapper.py  +12/-  atomic write, headers, raise_for_status
blfs_manager/commands.py     +339/-  resolver rewrite, download/install rework
blfs_manager/define.py         +6/-  DB_PATH, dead constant removed
blfs_manager/utils.py         +69/-  traversal+symlink guards, streaming MD5,
                                     exit-status propagation
tests/                        new    54 tests
.github/workflows/test.yml    new    Python 3.8-3.12 matrix
```

All existing functionality is preserved. Three behaviour changes are
intentional and are corrections of demonstrably wrong behaviour:

1. A failing build command now **aborts** the package instead of continuing.
2. `search()` for a missing package exits **1** instead of **0**.
3. Answers other than `y`/`n`/`m`/`q` are no longer treated as "yes".
