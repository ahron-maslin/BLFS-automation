"""Structural and semantic validation of the scraped BLFS dependency database.

The database is produced by scraping the BLFS book's HTML, so a change to the
book silently degrades it rather than failing loudly. Every downstream defect
found in review traced back to trusting the scrape. This module turns those
failure classes into explicit, machine-checkable assertions.

Run it directly against a database file:

    python -m blfs_manager.validate lfs-deps-11.3

It exits non-zero only when an ``error``-severity issue is found; warnings and
informational findings describe conditions that are real but expected in the
shipped book (patch URLs without their own MD5 sum, book cross-references used
as dependency names, and so on).
"""

import argparse
import json
import re
import sys
from collections import Counter, defaultdict

from .define import DbTypes

ERROR = 'error'
WARNING = 'warning'
INFO = 'info'

SEVERITIES = (ERROR, WARNING, INFO)

REQUIRED_KEYS = (
    DbTypes.NAME, DbTypes.URL, DbTypes.DEPS, DbTypes.COMMANDS,
    DbTypes.HASHES, DbTypes.KCONF, DbTypes.TYPE,
)

DEP_CLASSES = (DbTypes.REQUIRED, DbTypes.RECOMMENDED, DbTypes.OPTIONAL)

LIST_KEYS = (DbTypes.URL, DbTypes.COMMANDS, DbTypes.HASHES, DbTypes.KCONF)

PKG_TYPE_BLFS = 'BLFS'
PKG_TYPE_EXTERNAL = 'external'
VALID_PKG_TYPES = (PKG_TYPE_BLFS, PKG_TYPE_EXTERNAL)

MD5_RE = re.compile(r'^[0-9a-fA-F]{32}$')
FETCHABLE_URL_RE = re.compile(r'^(?:https?|ftp)://\S', re.IGNORECASE)


class Issue:
    """A single validation finding.

    Attributes:
        severity (str): One of ``error``, ``warning`` or ``info``.
        code (str): Stable machine-readable identifier for the check.
        package (str): The database key the finding concerns, or ``None`` for
            database-wide findings.
        message (str): Human-readable explanation.
    """

    __slots__ = ('severity', 'code', 'package', 'message')

    def __init__(self, severity, code, package, message):
        self.severity = severity
        self.code = code
        self.package = package
        self.message = message

    def __eq__(self, other):
        if not isinstance(other, Issue):
            return NotImplemented
        return self.as_dict() == other.as_dict()

    def __hash__(self):
        return hash((self.severity, self.code, self.package, self.message))

    def __repr__(self):
        return 'Issue({0}, {1}, {2!r}, {3!r})'.format(
            self.severity, self.code, self.package, self.message)

    def as_dict(self):
        """Returns the issue as a plain dictionary.

        Returns:
            dict: Keys ``severity``, ``code``, ``package`` and ``message``.
        """
        return {
            'severity': self.severity,
            'code': self.code,
            'package': self.package,
            'message': self.message,
        }


def default_database_path():
    """Resolves the database path to validate when none was given.

    Resolved on call rather than at import so the module stays importable
    regardless of how sibling modules locate state.

    Returns:
        str: Path to the database, which is not guaranteed to exist.
    """
    try:
        from . import paths
        return str(paths.db_path())
    except (ImportError, AttributeError, OSError):
        pass

    try:
        from . import define
        return str(define.DB_PATH)
    except (ImportError, AttributeError, OSError):
        return 'lfs-deps-11.3'


def load_database(path):
    """Loads a JSON database from disk.

    Args:
        path (str): Path to the database file.

    Returns:
        dict: The decoded database.

    Raises:
        OSError: If the file cannot be read.
        ValueError: If the file is not valid JSON or is not a JSON object.
    """
    with open(path, 'r') as handle:
        data = json.load(handle)

    if not isinstance(data, dict):
        raise ValueError(
            '{0}: database must be a JSON object, got {1}'.format(
                path, type(data).__name__))

    return data


def _is_str_list(value):
    """Returns True if value is a list whose members are all strings."""
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def _validate_entry_schema(name, entry):
    """Checks a single entry against the database schema.

    Args:
        name (str): The database key for the entry.
        entry: The value stored under that key.

    Returns:
        tuple: ``(issues, usable)`` where ``issues`` is a list of Issue and
        ``usable`` is True when the entry is structurally sound enough for the
        semantic checks to run against it.
    """
    if not isinstance(entry, dict):
        return [Issue(ERROR, 'not-a-mapping', name,
                      'entry is {0}, expected an object'.format(
                          type(entry).__name__))], False

    issues = []
    missing = [key for key in REQUIRED_KEYS if key not in entry]
    if missing:
        issues.append(Issue(ERROR, 'missing-keys', name,
                            'missing required key(s): {0}'.format(
                                ', '.join(sorted(missing)))))

    for key in LIST_KEYS:
        if key not in entry:
            continue
        value = entry[key]
        if not isinstance(value, list):
            issues.append(Issue(ERROR, 'bad-field-type', name,
                                "'{0}' is {1}, expected list".format(
                                    key, type(value).__name__)))
        elif key == DbTypes.HASHES:
            if not all(v is None or isinstance(v, str) for v in value):
                issues.append(Issue(ERROR, 'bad-field-type', name,
                                    "'hashes' must contain only strings or null"))
        elif not _is_str_list(value):
            issues.append(Issue(ERROR, 'bad-field-type', name,
                                "'{0}' must contain only strings".format(key)))

    if DbTypes.NAME in entry and not isinstance(entry[DbTypes.NAME], str):
        issues.append(Issue(ERROR, 'bad-field-type', name,
                            "'name' is {0}, expected str".format(
                                type(entry[DbTypes.NAME]).__name__)))

    if DbTypes.TYPE in entry:
        pkg_type = entry[DbTypes.TYPE]
        if pkg_type not in VALID_PKG_TYPES:
            issues.append(Issue(ERROR, 'bad-pkg-type', name,
                                'pkg_type {0!r} is not one of {1}'.format(
                                    pkg_type, '/'.join(VALID_PKG_TYPES))))

    deps_ok = False
    if DbTypes.DEPS in entry:
        deps = entry[DbTypes.DEPS]
        if not isinstance(deps, dict):
            issues.append(Issue(ERROR, 'bad-field-type', name,
                                "'deps' is {0}, expected object".format(
                                    type(deps).__name__)))
        else:
            absent = [c for c in DEP_CLASSES if c not in deps]
            if absent:
                issues.append(Issue(ERROR, 'missing-dep-classes', name,
                                    "'deps' missing class(es): {0}".format(
                                        ', '.join(sorted(absent)))))
            bad = [c for c in DEP_CLASSES
                   if c in deps and not _is_str_list(deps[c])]
            if bad:
                issues.append(Issue(ERROR, 'bad-field-type', name,
                                    "deps[{0}] must be a list of strings".format(
                                        ', '.join(sorted(bad)))))
            deps_ok = not absent and not bad

    usable = not missing and not any(i.code in ('bad-field-type', 'not-a-mapping')
                                     for i in issues)
    return issues, usable and deps_ok


def _validate_hashes(name, entry):
    """Checks recorded MD5 sums and their pairing with the URL list.

    A BLFS package records exactly one MD5 sum for its tarball while listing
    required patches as extra URLs, so more URLs than sums is expected and
    reported as informational. More sums than URLs means the pairing is broken.

    Args:
        name (str): The database key for the entry.
        entry (dict): The entry to check.

    Returns:
        list: Issues found.
    """
    issues = []
    urls = entry[DbTypes.URL]
    hashes = entry[DbTypes.HASHES]

    for value in hashes:
        if value is None:
            continue
        if not MD5_RE.match(value):
            issues.append(Issue(
                ERROR, 'bad-hash', name,
                'hash {0!r} is not a 32-character MD5 sum'.format(value)))

    if len(hashes) > len(urls):
        issues.append(Issue(
            ERROR, 'excess-hashes', name,
            '{0} hash(es) recorded for {1} URL(s); the pairing is broken'.format(
                len(hashes), len(urls))))
    elif len(urls) > len(hashes):
        issues.append(Issue(
            INFO, 'url-hash-count-mismatch', name,
            '{0} URL(s) but {1} hash(es); {2} download(s) cannot be '
            'verified (normally patches)'.format(
                len(urls), len(hashes), len(urls) - len(hashes))))

    return issues


def _validate_urls(name, entry):
    """Checks that every recorded URL is something a downloader can fetch.

    Args:
        name (str): The database key for the entry.
        entry (dict): The entry to check.

    Returns:
        list: Issues found.
    """
    issues = []
    urls = entry[DbTypes.URL]

    if not urls and entry.get(DbTypes.TYPE) == PKG_TYPE_BLFS:
        issues.append(Issue(
            WARNING, 'no-url', name,
            'BLFS package has no download URL; it cannot be downloaded or built'))

    for url in urls:
        if not FETCHABLE_URL_RE.match(url):
            issues.append(Issue(
                WARNING, 'unusable-url', name,
                'URL {0!r} is not an absolute http/https/ftp URL; it is most '
                'likely a scraped book cross-reference'.format(url)))

    if len(set(urls)) != len(urls):
        duplicated = sorted({u for u in urls if urls.count(u) > 1})
        issues.append(Issue(
            WARNING, 'duplicate-url', name,
            'URL list repeats: {0}'.format(', '.join(duplicated))))

    return issues


def _validate_deps(name, entry, known):
    """Checks dependency references for resolvability and hygiene.

    Args:
        name (str): The database key for the entry.
        entry (dict): The entry to check.
        known (set): Every key present in the database.

    Returns:
        tuple: ``(issues, unresolved)`` where ``unresolved`` maps each
        unresolvable dependency name to the set of packages referencing it.
    """
    issues = []
    unresolved = defaultdict(set)

    for dep_class in DEP_CLASSES:
        deps = entry[DbTypes.DEPS][dep_class]

        if len(set(deps)) != len(deps):
            repeated = sorted({d for d in deps if deps.count(d) > 1})
            issues.append(Issue(
                WARNING, 'duplicate-dependency', name,
                '{0} dependencies repeat: {1}'.format(
                    dep_class, ', '.join(repeated))))

        for dep in deps:
            if dep == name:
                issues.append(Issue(
                    WARNING, 'self-dependency', name,
                    'lists itself as a {0} dependency'.format(dep_class)))
            elif dep not in known:
                unresolved[dep].add(name)

    return issues, unresolved


def _validate_names(db):
    """Checks database keys for whitespace damage and key/name disagreement.

    ``strip_text()`` collapses newline-plus-indent runs to a single space but
    never trims, so an external dependency whose anchor text is wrapped in the
    book's HTML is stored under a key with a leading space.

    Args:
        db (dict): The database.

    Returns:
        list: Issues found.
    """
    issues = []

    for name, entry in sorted(db.items()):
        if not isinstance(name, str):
            issues.append(Issue(ERROR, 'bad-key-type', str(name),
                                'database key is {0}, expected str'.format(
                                    type(name).__name__)))
            continue

        if not name.strip():
            issues.append(Issue(ERROR, 'empty-name', name,
                                'database key is empty or whitespace only'))
            continue

        if name != name.strip():
            issues.append(Issue(
                WARNING, 'untrimmed-name', name,
                'key has leading/trailing whitespace; it will never match a '
                'user-typed or book-referenced name'))

        if isinstance(entry, dict) and isinstance(entry.get(DbTypes.NAME), str):
            if entry[DbTypes.NAME] != name:
                issues.append(Issue(
                    WARNING, 'key-name-mismatch', name,
                    "stored name is {0!r} but the key is {1!r}".format(
                        entry[DbTypes.NAME], name)))

    return issues


def _validate_aliases(db):
    """Reports keys that differ only by case or surrounding whitespace.

    The scraper keys external dependencies on the book's anchor text, which is
    inconsistently capitalised, so the same upstream project is recorded several
    times under names the resolver treats as unrelated packages.

    Args:
        db (dict): The database.

    Returns:
        list: Issues found.
    """
    groups = defaultdict(list)
    for name in db:
        if isinstance(name, str):
            groups[name.strip().lower()].append(name)

    return [
        Issue(INFO, 'alias-collision', None,
              'keys differ only by case/whitespace: {0}'.format(
                  ', '.join(repr(n) for n in sorted(names))))
        for _, names in sorted(groups.items()) if len(names) > 1
    ]


def validate_database(db):
    """Validates a scraped BLFS dependency database.

    Args:
        db (dict): The database, mapping package name to entry.

    Returns:
        list: Issue objects, ordered errors first, then warnings, then info.

    Raises:
        TypeError: If ``db`` is not a mapping.
    """
    if not isinstance(db, dict):
        raise TypeError(
            'database must be a dict, got {0}'.format(type(db).__name__))

    issues = []
    unresolved = defaultdict(set)
    known = set(db)

    for name, entry in sorted(db.items(), key=lambda kv: str(kv[0])):
        schema_issues, usable = _validate_entry_schema(str(name), entry)
        issues.extend(schema_issues)
        if not usable:
            continue

        issues.extend(_validate_hashes(str(name), entry))
        issues.extend(_validate_urls(str(name), entry))
        dep_issues, entry_unresolved = _validate_deps(str(name), entry, known)
        issues.extend(dep_issues)
        for dep, referrers in entry_unresolved.items():
            unresolved[dep].update(referrers)

    issues.extend(_validate_names(db))
    issues.extend(_validate_aliases(db))

    for dep in sorted(unresolved):
        referrers = sorted(unresolved[dep])
        sample = ', '.join(referrers[:3])
        if len(referrers) > 3:
            sample += ', ...'
        issues.append(Issue(
            WARNING, 'unknown-dependency', dep,
            'referenced as a dependency by {0} package(s) ({1}) but is not a '
            'database key; most likely a book cross-reference rather than a '
            'package'.format(len(referrers), sample)))

    order = {severity: i for i, severity in enumerate(SEVERITIES)}
    issues.sort(key=lambda i: (order[i.severity], i.code, i.package or ''))
    return issues


def summarise(issues):
    """Counts issues by severity.

    Args:
        issues (list): Issue objects.

    Returns:
        dict: Mapping of every severity name to its count, zeros included.
    """
    counts = {severity: 0 for severity in SEVERITIES}
    for issue in issues:
        counts[issue.severity] += 1
    return counts


def format_report(issues, db=None, source=None, limit=10):
    """Renders a human-readable report.

    Args:
        issues (list): Issue objects.
        db (dict): The validated database, used for the header counts.
        source (str): Path the database was read from, for the header.
        limit (int): Maximum issues listed per code; ``None`` lists all.

    Returns:
        str: The formatted report.
    """
    lines = []
    if source is not None:
        lines.append('BLFS database validation: {0}'.format(source))
    if isinstance(db, dict):
        types = Counter(
            entry.get(DbTypes.TYPE, 'unknown') if isinstance(entry, dict)
            else 'malformed' for entry in db.values())
        breakdown = ', '.join(
            '{0} {1}'.format(count, kind) for kind, count in sorted(types.items()))
        lines.append('  {0} entries ({1})'.format(len(db), breakdown))
    if lines:
        lines.append('')

    for severity in SEVERITIES:
        selected = [i for i in issues if i.severity == severity]
        if not selected:
            continue
        lines.append('{0} ({1})'.format(severity.upper(), len(selected)))

        by_code = defaultdict(list)
        for issue in selected:
            by_code[issue.code].append(issue)

        for code in sorted(by_code):
            group = by_code[code]
            lines.append('  {0} ({1})'.format(code, len(group)))
            shown = group if limit is None else group[:limit]
            for issue in shown:
                where = issue.package if issue.package else '<database>'
                lines.append('    {0}: {1}'.format(where, issue.message))
            if len(group) > len(shown):
                lines.append('    ... and {0} more'.format(len(group) - len(shown)))
        lines.append('')

    counts = summarise(issues)
    lines.append('Summary: {0} error(s), {1} warning(s), {2} informational'.format(
        counts[ERROR], counts[WARNING], counts[INFO]))
    if counts[ERROR] == 0:
        lines.append('OK: no errors.')
    return '\n'.join(lines)


def main(argv=None):
    """Command-line entry point.

    Args:
        argv (list): Argument vector, defaulting to ``sys.argv[1:]``.

    Returns:
        int: 0 when no error-severity issue was found, 1 when at least one was,
        and 2 when the database could not be read or decoded.
    """
    parser = argparse.ArgumentParser(
        prog='python -m blfs_manager.validate',
        description='Validate a scraped BLFS dependency database.')
    parser.add_argument('database', nargs='?', default=None,
                        help='path to the database file '
                             '(default: the installed database)')
    parser.add_argument('--all', action='store_true',
                        help='list every issue instead of the first 10 per code')
    parser.add_argument('--quiet', action='store_true',
                        help='print only the summary line')
    parser.add_argument('--json', action='store_true',
                        help='emit findings as JSON instead of a text report')
    args = parser.parse_args(argv)
    source = args.database or default_database_path()

    try:
        db = load_database(source)
    except (OSError, ValueError) as exc:
        print('cannot validate: {0}'.format(exc), file=sys.stderr)
        return 2

    issues = validate_database(db)
    counts = summarise(issues)

    if args.json:
        print(json.dumps({
            'source': source,
            'entries': len(db),
            'counts': counts,
            'issues': [i.as_dict() for i in issues],
        }, indent=2))
    elif args.quiet:
        print('Summary: {0} error(s), {1} warning(s), {2} informational'.format(
            counts[ERROR], counts[WARNING], counts[INFO]))
    else:
        print(format_report(issues, db, source,
                            limit=None if args.all else 10))

    return 1 if counts[ERROR] else 0


if __name__ == '__main__':
    sys.exit(main())
