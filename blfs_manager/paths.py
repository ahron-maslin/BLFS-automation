"""Resolution of every filesystem location blfs-pm writes to.

Historically the database, the install log and the source cache all lived
inside the installed package directory (``site-packages/``). That requires
root to write a library directory, is destroyed by a ``pip`` upgrade, and
makes the state impossible to relocate. This module resolves those three
things to proper system locations, seeds the database from the read-only copy
shipped in the wheel, and migrates any legacy state left behind by an older
release.

Locations are resolved by *functions*, never by import-time constants, so that
the environment can change between calls without the package being reimported.

Resolution order:
    1. ``BLFS_PM_STATE_DIR`` / ``BLFS_PM_CACHE_DIR`` if set.
    2. ``/var/lib/blfs-pm`` and ``/var/cache/blfs-pm/sources`` when running as
       root, which is the documented way to run this tool.
    3. XDG fallbacks for an unprivileged run.
"""

import logging
import os
import shutil
import tempfile
from pathlib import Path

STATE_DIR_ENV = 'BLFS_PM_STATE_DIR'
CACHE_DIR_ENV = 'BLFS_PM_CACHE_DIR'

ROOT_STATE_DIR = Path('/var/lib/blfs-pm')
ROOT_CACHE_DIR = Path('/var/cache/blfs-pm/sources')

APP_DIRNAME = 'blfs-pm'
SOURCES_DIRNAME = 'sources'

DB_GLOB = 'lfs-deps-*'
DEFAULT_DB_FILENAME = 'lfs-deps-11.3'
INSTALLED_LOG_NAME = '.installed_log'
JOURNAL_NAME = 'build.jsonl'
LEGACY_SOURCES_DIRNAME = 'blfs_sources'

DIR_MODE = 0o755

PACKAGE_DIR = Path(__file__).resolve().parent
LEGACY_ROOT = PACKAGE_DIR.parent

_CHECKOUT_MARKERS = ('setup.py', 'pyproject.toml', '.git')


def is_root():
    """
    Report whether the current process has an effective uid of 0.

    Returns:
        bool: True when running as root, False otherwise (including on
            platforms without ``geteuid``).
    """
    if not hasattr(os, 'geteuid'):
        return False
    return os.geteuid() == 0


def _env_dir(name):
    """
    Read a directory path from an environment variable.

    Args:
        name (str): The environment variable name.

    Returns:
        pathlib.Path | None: The expanded path, or None when the variable is
            unset or empty.
    """
    value = os.environ.get(name, '').strip()
    if not value:
        return None
    return Path(value).expanduser()


def _xdg_base(name, fallback):
    """
    Resolve an XDG base directory.

    Args:
        name (str): The XDG environment variable name, e.g. ``XDG_STATE_HOME``.
        fallback (str): Path relative to the home directory used when the
            variable is unset or not absolute.

    Returns:
        pathlib.Path: The resolved base directory.
    """
    value = os.environ.get(name, '').strip()
    if value:
        # The XDG spec requires an absolute path; a relative value is invalid
        # and must be ignored rather than resolved against the caller's CWD.
        candidate = Path(value).expanduser()
        if candidate.is_absolute():
            return candidate
    return Path.home() / fallback


def state_dir():
    """
    Locate the directory holding the database and the install log.

    No directory is created and nothing is touched on disk.

    Returns:
        pathlib.Path: The state directory for this user.
    """
    override = _env_dir(STATE_DIR_ENV)
    if override is not None:
        return override
    if is_root():
        return ROOT_STATE_DIR
    return _xdg_base('XDG_STATE_HOME', '.local/state') / APP_DIRNAME


def sources_dir():
    """
    Locate the directory holding downloaded source tarballs and patches.

    ``BLFS_PM_CACHE_DIR`` is used verbatim when set - no ``sources``
    subdirectory is appended - so the caller keeps full control of the layout.
    No directory is created.

    Returns:
        pathlib.Path: The source cache directory for this user.
    """
    override = _env_dir(CACHE_DIR_ENV)
    if override is not None:
        return override
    if is_root():
        return ROOT_CACHE_DIR
    return _xdg_base('XDG_CACHE_HOME', '.cache') / APP_DIRNAME / SOURCES_DIRNAME


def installed_log_path():
    """
    Locate the file recording which packages have been built.

    Returns:
        pathlib.Path: Path to the install log inside the state directory.
    """
    return state_dir() / INSTALLED_LOG_NAME


def journal_path():
    """
    Locate the crash-safe build journal.

    Returns:
        pathlib.Path: Path to the journal inside the state directory.
    """
    return state_dir() / JOURNAL_NAME


def _databases_in(directory):
    """
    List database files in a directory, newest name last.

    Args:
        directory (pathlib.Path): The directory to scan.

    Returns:
        list[pathlib.Path]: Matching regular files, sorted by name.
    """
    try:
        found = [p for p in directory.glob(DB_GLOB)
                 if p.is_file() and not p.name.endswith('.tmp')]
    except OSError:
        return []
    return sorted(found, key=lambda p: p.name)


def packaged_db_path(package_dir=None):
    """
    Locate the read-only database shipped inside the installed package.

    Args:
        package_dir (pathlib.Path | str | None): Directory to search. Defaults
            to the directory containing this module.

    Returns:
        pathlib.Path | None: The packaged database, or None when no seed
            shipped (in which case the caller has to scrape the book).
    """
    base = Path(package_dir) if package_dir is not None else PACKAGE_DIR
    found = _databases_in(base)
    return found[-1] if found else None


def db_path():
    """
    Locate the writable database, without creating or seeding anything.

    The packaged seed's filename wins because it names the book edition this
    release was built against; a database already present in the state
    directory is only used when nothing shipped.

    Returns:
        pathlib.Path: Path to the writable database. It may not exist yet.
    """
    state = state_dir()
    seed = packaged_db_path()
    if seed is not None:
        return state / seed.name
    existing = _databases_in(state)
    if existing:
        return existing[-1]
    return state / DEFAULT_DB_FILENAME


def ensure_writable(path, mode=DIR_MODE):
    """
    Guarantee that a directory exists and can actually be written to.

    Call this before any expensive work. Without it an unprivileged run
    scrapes ~1600 book pages and only discovers it cannot write the result
    afterwards.

    Args:
        path (pathlib.Path | str): The directory to check or create.
        mode (int): Permission bits used when creating the directory.

    Returns:
        pathlib.Path: The directory, guaranteed to exist and be writable.

    Raises:
        NotADirectoryError: If the path exists but is not a directory.
        PermissionError: If the directory cannot be created or written to.
    """
    target = Path(path)

    if target.exists() and not target.is_dir():
        raise NotADirectoryError(
            f'{target} exists but is not a directory.\n'
            f'Remove it, or point {STATE_DIR_ENV}/{CACHE_DIR_ENV} elsewhere.')

    if not target.is_dir():
        try:
            target.mkdir(mode=mode, parents=True, exist_ok=True)
        except OSError as exc:
            raise PermissionError(
                f'Cannot create {target}: {exc}.\n'
                f'Run blfs-pm as root, or set {STATE_DIR_ENV} and '
                f'{CACHE_DIR_ENV} to directories you own.') from exc

    try:
        with tempfile.TemporaryFile(dir=str(target)):
            pass
    except OSError as exc:
        raise PermissionError(
            f'{target} is not writable: {exc}.\n'
            f'Run blfs-pm as root, or set {STATE_DIR_ENV} and '
            f'{CACHE_DIR_ENV} to directories you own.') from exc

    return target


def ensure_state_dir():
    """
    Create the state directory and verify it is writable.

    Returns:
        pathlib.Path: The state directory.

    Raises:
        PermissionError: If it cannot be created or written to.
    """
    return ensure_writable(state_dir())


def ensure_sources_dir():
    """
    Create the source cache directory and verify it is writable.

    Returns:
        pathlib.Path: The source cache directory.

    Raises:
        PermissionError: If it cannot be created or written to.
    """
    return ensure_writable(sources_dir())


def ensure_db(seed=True):
    """
    Return the writable database path, seeding it from the packaged copy.

    A fresh install would otherwise scrape the whole book on first run. When
    no seed shipped, the returned path simply does not exist and the caller is
    expected to scrape into it.

    Args:
        seed (bool): Whether to copy the packaged database when the writable
            one is missing.

    Returns:
        pathlib.Path: Path to the writable database, which exists if and only
            if it was already present or a seed was available.

    Raises:
        PermissionError: If seeding is required but the state directory is not
            writable.
    """
    target = db_path()
    if target.exists():
        return target

    source = packaged_db_path() if seed else None
    if source is None:
        logging.debug('No packaged database available - %s must be scraped.',
                      target)
        return target

    ensure_writable(target.parent)

    # Copy through a temporary name so an interrupted seed cannot leave a
    # truncated database that later runs would load as valid JSON input.
    tmp = target.parent / f'{target.name}.seed-tmp'
    try:
        shutil.copy2(source, tmp)
        os.replace(tmp, target)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
    logging.info('Seeded package database from %s to %s', source, target)
    return target


def _is_source_checkout(root):
    """
    Report whether a directory looks like a development checkout.

    Args:
        root (pathlib.Path): The directory to inspect.

    Returns:
        bool: True if the directory carries a project marker file.
    """
    return any((root / marker).exists() for marker in _CHECKOUT_MARKERS)


def _relocate(source, destination, copy_only):
    """
    Move or copy a file or directory to a destination that does not exist.

    Args:
        source (pathlib.Path): The item to relocate.
        destination (pathlib.Path): The non-existent destination path.
        copy_only (bool): Copy instead of moving, leaving the source in place.

    Raises:
        OSError: If neither the move nor the copy succeeds.
    """
    if not copy_only:
        try:
            shutil.move(str(source), str(destination))
            return
        except OSError as exc:
            logging.warning('Could not move %s (%s) - copying instead.',
                            source, exc)

    if source.is_dir():
        shutil.copytree(source, destination)
    else:
        shutil.copy2(source, destination)


def _legacy_candidates(legacy_root, state, sources):
    """
    Enumerate legacy state files and their new destinations.

    Args:
        legacy_root (pathlib.Path): The old state location.
        state (pathlib.Path): The new state directory.
        sources (pathlib.Path): The new source cache directory.

    Returns:
        list[tuple[pathlib.Path, pathlib.Path]]: (source, destination) pairs.
    """
    candidates = []

    legacy_log = legacy_root / INSTALLED_LOG_NAME
    if legacy_log.is_file():
        candidates.append((legacy_log, state / INSTALLED_LOG_NAME))

    for database in _databases_in(legacy_root):
        candidates.append((database, state / database.name))

    legacy_sources = legacy_root / LEGACY_SOURCES_DIRNAME
    if legacy_sources.is_dir():
        for entry in sorted(legacy_sources.iterdir(), key=lambda p: p.name):
            candidates.append((entry, sources / entry.name))

    return candidates


def migrate_legacy_state(legacy_root=None, state=None, sources=None):
    """
    Move state written by older releases into the new locations, once.

    An existing file at the destination is never overwritten, which makes the
    migration idempotent and protects an install log recording a system the
    user has already built. Failures are reported and skipped rather than
    aborting the run.

    Args:
        legacy_root (pathlib.Path | str | None): The old state location.
            Defaults to the directory containing the installed package.
        state (pathlib.Path | str | None): Destination state directory.
            Defaults to :func:`state_dir`.
        sources (pathlib.Path | str | None): Destination source cache.
            Defaults to :func:`sources_dir`.

    Returns:
        list[tuple[pathlib.Path, pathlib.Path]]: The (source, destination)
            pairs actually migrated by this call. Empty on a second run.
    """
    root = Path(legacy_root) if legacy_root is not None else LEGACY_ROOT
    new_state = Path(state) if state is not None else state_dir()
    new_sources = Path(sources) if sources is not None else sources_dir()

    if not root.is_dir():
        return []

    candidates = _legacy_candidates(root, new_state, new_sources)
    if not candidates:
        return []

    # Under `pip install -e .` the legacy root is the developer's checkout;
    # moving files out of it would gut the working tree, so copy there.
    copy_only = _is_source_checkout(root)

    migrated = []
    for source, destination in candidates:
        if destination.exists():
            logging.debug('Legacy state %s already present at %s - skipping.',
                          source, destination)
            continue
        try:
            ensure_writable(destination.parent)
            _relocate(source, destination, copy_only)
        except OSError as exc:
            logging.error('Could not migrate %s to %s: %s',
                          source, destination, exc)
            continue
        logging.info('%s %s -> %s',
                     'Copied' if copy_only else 'Migrated', source, destination)
        migrated.append((source, destination))

    legacy_sources = root / LEGACY_SOURCES_DIRNAME
    if not copy_only and legacy_sources.is_dir():
        try:
            legacy_sources.rmdir()
        except OSError:
            pass

    if migrated:
        logging.info('%s %d legacy item(s) %s %s.',
                     'Copied' if copy_only else 'Migrated', len(migrated),
                     'from' if copy_only else 'out of', root)
    return migrated


def preflight(sources=True, migrate=True):
    """
    Prepare and validate every writable location before any expensive work.

    Args:
        sources (bool): Also prepare the source cache directory.
        migrate (bool): Also run the legacy state migration.

    Returns:
        tuple[pathlib.Path, pathlib.Path | None]: The state directory and, when
            requested, the source cache directory.

    Raises:
        PermissionError: If a required directory is missing and cannot be
            created, or exists but is not writable.
    """
    state = ensure_state_dir()
    cache = ensure_sources_dir() if sources else None
    if migrate:
        migrate_legacy_state()
    return state, cache
