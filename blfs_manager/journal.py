"""Crash-safe build journal for blfs-pm.

A BLFS desktop build is 100+ packages over many hours; a failure at package 80
must not send the user back to package 1. This module records every build phase
to an append-only JSON Lines file so a later run can answer "what was I doing
and what is left".

Each event is a single JSON object on its own line, written with one ``write()``
syscall to a file opened ``O_APPEND`` and ``fsync``ed immediately. Power loss or
``SIGKILL`` can therefore only ever damage the final line, and the reader skips
unparsable lines instead of failing.

Event schema (every event carries these)::

    v          int   schema version
    ts         str   ISO 8601, timezone-aware UTC
    event      str   one of Phase.*
    queue_id   str   present for every event belonging to a build queue

Per-phase fields::

    queue_started      target, queue[], metadata{}
    queue_resumed      target, remaining
    package_started    package
    package_extracted  package, directory
    command_run        package, command, status
    package_completed  package
    package_failed     package, status, reason
    package_skipped    package, reason
    queue_completed    status, completed_count, failed_count
    queue_aborted      reason

Resuming a build::

    journal = BuildJournal(path)
    state = journal.resume_state()
    if state is not None:
        journal.resume_queue()
        for pkg in state.remaining:
            ...
"""

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from termcolor import colored

SCHEMA_VERSION = 1

DEFAULT_MAX_BYTES = 5 * 1024 * 1024

DEFAULT_BACKUP_COUNT = 3


class Phase:
    """Event names written to the journal."""

    QUEUE_STARTED = 'queue_started'
    QUEUE_RESUMED = 'queue_resumed'
    QUEUE_COMPLETED = 'queue_completed'
    QUEUE_ABORTED = 'queue_aborted'
    PACKAGE_STARTED = 'package_started'
    PACKAGE_EXTRACTED = 'package_extracted'
    COMMAND_RUN = 'command_run'
    PACKAGE_COMPLETED = 'package_completed'
    PACKAGE_FAILED = 'package_failed'
    PACKAGE_SKIPPED = 'package_skipped'


TERMINAL_QUEUE_PHASES = frozenset({Phase.QUEUE_COMPLETED, Phase.QUEUE_ABORTED})

# Phases that must survive rotation for --resume to keep working. Command and
# extraction events are history only, so rotation may drop them.
RESUME_PHASES = frozenset({
    Phase.QUEUE_STARTED,
    Phase.QUEUE_RESUMED,
    Phase.PACKAGE_COMPLETED,
    Phase.PACKAGE_SKIPPED,
})

OUTCOME_COMPLETED = 'completed'
OUTCOME_FAILED = 'failed'
OUTCOME_ABORTED = 'aborted'
OUTCOME_UNFINISHED = 'unfinished'


def utc_now():
    """Returns the current time as a timezone-aware UTC ISO 8601 string.

    Returns:
        str: e.g. ``2026-07-27T09:15:00.123456+00:00``.

    """
    return datetime.now(timezone.utc).isoformat()


def parse_timestamp(value):
    """Parses a journal timestamp.

    Args:
        value (str): An ISO 8601 timestamp, or anything else.

    Returns:
        datetime.datetime: The parsed timestamp, or None if it is unusable.

    """
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _new_queue_id():
    """Returns a sortable, collision-resistant identifier for a build queue.

    Returns:
        str: e.g. ``20260727T091500-1a2b3c4d``.

    """
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')
    return f'{stamp}-{os.urandom(4).hex()}'


@dataclass
class ResumeState:
    """Everything a caller needs to restart an interrupted build queue.

    Attributes:
        queue_id (str): Identifier of the unfinished queue.
        target (str): The package the user originally asked to build.
        queue (list): The full resolved build order recorded at queue start.
        completed (list): Packages that finished successfully.
        skipped (list): Packages deliberately not built (manual, non-BLFS).
        in_progress (str): Package being built when the journal stopped, if any.
        failed (str): Package whose build failed, if any.
        started_at (str): Timestamp of the queue_started event.
        last_event_at (str): Timestamp of the most recent event for this queue.

    """

    queue_id: str
    target: str
    queue: list = field(default_factory=list)
    completed: list = field(default_factory=list)
    skipped: list = field(default_factory=list)
    in_progress: str = None
    failed: str = None
    started_at: str = None
    last_event_at: str = None

    @property
    def remaining(self):
        """Returns the queue entries still to build, in build order.

        A package that was in progress or that failed is still remaining, so
        the resumed run retries it rather than stepping over the failure.

        Returns:
            list: Package names.

        """
        done = set(self.completed) | set(self.skipped)
        return [pkg for pkg in self.queue if pkg not in done]

    @property
    def resume_point(self):
        """Returns the package a resumed build should start at.

        Returns:
            str: A package name, or None if nothing is left.

        """
        remaining = self.remaining
        return remaining[0] if remaining else None


@dataclass
class BuildSummary:
    """A single build queue, summarised for history display.

    Attributes:
        queue_id (str): Identifier of the queue.
        target (str): The package the user asked to build.
        outcome (str): One of OUTCOME_COMPLETED, OUTCOME_FAILED,
            OUTCOME_ABORTED or OUTCOME_UNFINISHED.
        queued (int): Number of packages in the resolved queue.
        completed (list): Packages built successfully.
        skipped (list): Packages not built.
        failed (list): Packages whose build failed.
        started_at (str): Timestamp of the queue_started event.
        finished_at (str): Timestamp of the terminal event, if any.
        duration_seconds (float): Wall-clock duration, if the queue finished.
        reason (str): Abort reason, if the queue was aborted.

    """

    queue_id: str
    target: str
    outcome: str
    queued: int = 0
    completed: list = field(default_factory=list)
    skipped: list = field(default_factory=list)
    failed: list = field(default_factory=list)
    started_at: str = None
    finished_at: str = None
    duration_seconds: float = None
    reason: str = None


class BuildJournal(object):
    """Append-only, crash-safe record of build progress.

    Args:
        path (str or pathlib.Path): Journal file. Parent directories are
            created on first write.
        max_bytes (int): Rotate once the journal reaches this size. Zero or
            less disables rotation.
        backup_count (int): How many rotated journals to keep.

    Attributes:
        path (pathlib.Path): Journal file.
        max_bytes (int): Rotation threshold in bytes.
        backup_count (int): Number of rotated journals kept.

    """

    def __init__(self, path, max_bytes=DEFAULT_MAX_BYTES,
                 backup_count=DEFAULT_BACKUP_COUNT):
        """Initializes the journal without touching the filesystem.

        Args:
            path (str or pathlib.Path): Journal file.
            max_bytes (int): Rotation threshold in bytes.
            backup_count (int): Number of rotated journals to keep.

        """
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.backup_count = max(0, backup_count)
        self._queue_id = None
        self._rotating = False

    @property
    def active_queue_id(self):
        """Returns the queue this instance is currently writing events for.

        Returns:
            str: A queue identifier, or None if no queue is active.

        """
        return self._queue_id

    def exists(self):
        """Reports whether the journal file is present.

        Returns:
            bool: True if the journal exists on disk.

        """
        return self.path.is_file()

    def start_queue(self, target, queue, metadata=None):
        """Records the start of a build queue and its full resolved order.

        The resolved build order is deterministic, so recording it once here
        and replaying completions against it is enough to resume later.

        Args:
            target (str): The package the user asked to build.
            queue (iterable): The resolved build order.
            metadata (dict): Optional extra context (flags, book version).

        Returns:
            str: The identifier of the new queue.

        """
        self._queue_id = _new_queue_id()
        self._append(
            Phase.QUEUE_STARTED,
            target=target,
            queue=list(queue),
            metadata=dict(metadata or {}),
        )
        return self._queue_id

    def resume_queue(self):
        """Re-attaches this journal to the unfinished queue, if there is one.

        Subsequent package events are recorded against the original queue, so
        a build that is resumed several times still reads back as one build.

        Returns:
            ResumeState: The state to resume from, or None if there is nothing
            to resume.

        """
        state = self.resume_state()
        if state is None:
            return None

        self._queue_id = state.queue_id
        self._append(
            Phase.QUEUE_RESUMED,
            target=state.target,
            remaining=len(state.remaining),
        )
        return state

    def start_package(self, pkg):
        """Records that a package build has begun.

        Args:
            pkg (str): The package name.

        Returns:
            dict: The recorded event.

        """
        return self._append(Phase.PACKAGE_STARTED, package=pkg)

    def record_extracted(self, pkg, directory=None):
        """Records that a package source tree was extracted.

        Args:
            pkg (str): The package name.
            directory (str): The build directory, if known.

        Returns:
            dict: The recorded event.

        """
        return self._append(
            Phase.PACKAGE_EXTRACTED, package=pkg,
            directory=str(directory) if directory is not None else None)

    def record_command(self, pkg, command, status):
        """Records a build command and its exit status.

        Args:
            pkg (str): The package the command belongs to.
            command (str): The command as it was run.
            status (int): The exit status; 0 means success.

        Returns:
            dict: The recorded event.

        """
        return self._append(
            Phase.COMMAND_RUN, package=pkg, command=command, status=status)

    def complete_package(self, pkg):
        """Records a successful package build.

        Args:
            pkg (str): The package name.

        Returns:
            dict: The recorded event.

        """
        return self._append(Phase.PACKAGE_COMPLETED, package=pkg)

    def fail_package(self, pkg, status=None, reason=None):
        """Records a failed package build.

        Args:
            pkg (str): The package name.
            status (int): Exit status of the command that failed, if any.
            reason (str): Human-readable explanation, if any.

        Returns:
            dict: The recorded event.

        """
        return self._append(
            Phase.PACKAGE_FAILED, package=pkg, status=status, reason=reason)

    def skip_package(self, pkg, reason=None):
        """Records a package that was deliberately not built.

        Skipped packages are treated as resolved: they are excluded from the
        remaining queue so a resumed run does not stall on them again.

        Args:
            pkg (str): The package name.
            reason (str): Why it was skipped (already installed, book section,
                not a BLFS package).

        Returns:
            dict: The recorded event.

        """
        return self._append(Phase.PACKAGE_SKIPPED, package=pkg, reason=reason)

    def complete_queue(self, status=None):
        """Records that the active queue reached its end.

        Args:
            status (str): Overridden outcome. When omitted it is derived from
                the recorded package events.

        Returns:
            dict: The recorded event, or None if no queue is active.

        """
        if self._queue_id is None:
            logging.debug('complete_queue() called with no active build queue.')
            return None

        group = self._current_group()
        completed, skipped, failed = self._package_outcomes(group)
        if status is None:
            status = OUTCOME_FAILED if failed else OUTCOME_COMPLETED

        event = self._append(
            Phase.QUEUE_COMPLETED,
            status=status,
            completed_count=len(completed),
            skipped_count=len(skipped),
            failed_count=len(failed),
        )
        self._queue_id = None
        return event

    def abort_queue(self, reason=None):
        """Records that the active queue was abandoned before its end.

        Args:
            reason (str): Why the queue was abandoned (interrupted, failure).

        Returns:
            dict: The recorded event, or None if no queue is active.

        """
        if self._queue_id is None:
            logging.debug('abort_queue() called with no active build queue.')
            return None

        event = self._append(Phase.QUEUE_ABORTED, reason=reason)
        self._queue_id = None
        return event

    def read_events(self, include_rotated=False):
        """Reads every well-formed event from the journal.

        Args:
            include_rotated (bool): Also read rotated journals, oldest first.

        Returns:
            list: Event dictionaries in the order they were written.

        """
        return list(self.iter_events(include_rotated))

    def iter_events(self, include_rotated=False):
        """Yields well-formed events, tolerating a damaged journal.

        Unparsable lines -- the truncated final line left by a power cut, or
        anything else that is not a JSON object -- are skipped rather than
        raised.

        Args:
            include_rotated (bool): Also read rotated journals, oldest first.

        Yields:
            dict: One event.

        """
        for path in self._read_paths(include_rotated):
            yield from self._read_file(path)

    def resume_state(self):
        """Returns the state of the most recent unfinished build queue.

        Returns:
            ResumeState: The queue to resume, or None if every recorded queue
            reached a terminal event (or none was ever recorded).

        """
        groups = self._queue_groups(self.iter_events())

        for queue_id in reversed(list(groups)):
            group = groups[queue_id]
            if any(event['event'] in TERMINAL_QUEUE_PHASES for event in group):
                continue

            start = next(
                (e for e in group if e['event'] == Phase.QUEUE_STARTED), None)
            if start is None or not isinstance(start.get('queue'), list):
                # A queue whose start event was lost to a truncated write
                # cannot be replayed; treat it as unresumable rather than
                # guessing at a build order.
                continue

            return self._state_from_group(queue_id, group, start)

        return None

    def history(self, limit=10, include_rotated=False):
        """Summarises recorded builds, most recent first.

        Args:
            limit (int): Maximum number of builds to return; None for all.
            include_rotated (bool): Also read rotated journals.

        Returns:
            list: BuildSummary objects, newest first.

        """
        groups = self._queue_groups(self.iter_events(include_rotated))
        summaries = []

        for queue_id, group in groups.items():
            start = next(
                (e for e in group if e['event'] == Phase.QUEUE_STARTED), None)
            if start is None:
                continue
            summaries.append(self._summarise(queue_id, group, start))

        summaries.reverse()
        return summaries if limit is None else summaries[:limit]

    def print_history(self, limit=10, include_rotated=False):
        """Logs a human-readable build history.

        Args:
            limit (int): Maximum number of builds to show.
            include_rotated (bool): Also read rotated journals.

        Returns:
            list: The BuildSummary objects that were shown.

        """
        summaries = self.history(limit, include_rotated)
        if not summaries:
            logging.info(colored('No builds recorded yet.', 'blue'))
            return summaries

        colours = {
            OUTCOME_COMPLETED: 'green',
            OUTCOME_FAILED: 'red',
            OUTCOME_ABORTED: 'yellow',
            OUTCOME_UNFINISHED: 'yellow',
        }
        logging.info(colored('Recent builds:\n', 'green'))
        for summary in summaries:
            duration = ('' if summary.duration_seconds is None
                        else f' in {summary.duration_seconds:.0f}s')
            logging.info(colored(
                f'{summary.started_at}  {summary.target}  '
                f'[{summary.outcome}]{duration}',
                colours.get(summary.outcome, 'blue'), attrs=['bold']))
            logging.info(
                f'  {len(summary.completed)}/{summary.queued} built, '
                f'{len(summary.skipped)} skipped, {len(summary.failed)} failed')
            for pkg in summary.failed:
                logging.info(colored(f'  failed: {pkg}', 'red'))
        return summaries

    def clear(self):
        """Deletes the journal and every rotated copy.

        Returns:
            None

        """
        for path in [self.path] + self._backup_paths():
            try:
                path.unlink()
            except FileNotFoundError:
                continue
            except OSError as exc:
                logging.error(colored(
                    f'Could not remove build journal {path}: {exc}', 'red'))

    def _append(self, phase, **fields):
        """Builds an event and appends it to the journal.

        Args:
            phase (str): One of Phase.*.
            **fields: Phase-specific fields.

        Returns:
            dict: The event that was written.

        """
        event = {'v': SCHEMA_VERSION, 'ts': utc_now(), 'event': phase}
        if self._queue_id is not None:
            event['queue_id'] = self._queue_id
        event.update(fields)
        self._write_event(event)
        return event

    def _write_event(self, event):
        """Rotates if needed, then appends one event as a single line.

        Args:
            event (dict): The event to write.

        Returns:
            bool: True if the event reached the disk.

        """
        if not self._rotating:
            self._maybe_rotate()
        payload = (json.dumps(event, ensure_ascii=False) + '\n').encode('utf-8')
        return self._write_line(payload)

    def _write_line(self, payload):
        """Appends raw bytes with a single write syscall, then fsyncs.

        O_APPEND plus one write() keeps concurrent writers from interleaving
        partial lines, and the fsync is what makes the record survive power
        loss. A journal that cannot be written must not abort a build, so
        failures are logged rather than raised.

        Args:
            payload (bytes): One complete line, newline included.

        Returns:
            bool: True if the write and fsync succeeded.

        """
        created = not self.path.exists()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = os.open(
                self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
            try:
                os.write(handle, payload)
                os.fsync(handle)
            finally:
                os.close(handle)
        except OSError as exc:
            logging.error(colored(
                f'Could not write build journal {self.path} ({exc}) - '
                f'resume information will be incomplete.', 'red'))
            return False

        if created:
            self._sync_directory()
        return True

    def _sync_directory(self):
        """Flushes the parent directory so a newly created journal survives.

        Returns:
            None

        """
        try:
            handle = os.open(self.path.parent, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(handle)
        except OSError:
            pass
        finally:
            os.close(handle)

    def _read_paths(self, include_rotated):
        """Returns the journal files to read, oldest first.

        Args:
            include_rotated (bool): Include rotated journals.

        Returns:
            list: pathlib.Path objects.

        """
        if not include_rotated:
            return [self.path]
        return list(reversed(self._backup_paths())) + [self.path]

    def _backup_paths(self):
        """Returns rotated journal paths, newest first.

        Returns:
            list: pathlib.Path objects, existing files only.

        """
        paths = [Path(f'{self.path}.{index}')
                 for index in range(1, self.backup_count + 1)]
        return [path for path in paths if path.is_file()]

    def _read_file(self, path):
        """Yields the well-formed events in one journal file.

        Args:
            path (pathlib.Path): The file to read.

        Yields:
            dict: One event.

        """
        try:
            handle = open(path, 'r', encoding='utf-8', errors='replace')
        except OSError:
            return

        with handle:
            for number, line in enumerate(handle, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    logging.debug(
                        'Skipping unreadable build-journal line %s:%d '
                        '(expected after an interrupted build).', path, number)
                    continue
                if isinstance(event, dict) and isinstance(event.get('event'), str):
                    yield event

    def _queue_groups(self, events):
        """Groups events by queue, preserving first-seen queue order.

        Args:
            events (iterable): Events to group.

        Returns:
            dict: queue_id -> list of events.

        """
        groups = {}
        for event in events:
            queue_id = event.get('queue_id')
            if isinstance(queue_id, str):
                groups.setdefault(queue_id, []).append(event)
        return groups

    def _current_group(self):
        """Returns the recorded events of the active queue.

        Returns:
            list: Events, empty if no queue is active.

        """
        if self._queue_id is None:
            return []
        return self._queue_groups(self.iter_events()).get(self._queue_id, [])

    def _package_outcomes(self, group):
        """Replays package events into completed/skipped/failed sets.

        Args:
            group (list): Events belonging to one queue, in order.

        Returns:
            tuple: (completed, skipped, failed) lists of package names.

        """
        completed, skipped, failed = [], [], []

        for event in group:
            phase = event['event']
            pkg = event.get('package')
            if not isinstance(pkg, str):
                continue

            if phase == Phase.PACKAGE_COMPLETED:
                if pkg in skipped:
                    skipped.remove(pkg)
                if pkg in failed:
                    failed.remove(pkg)
                if pkg not in completed:
                    completed.append(pkg)
            elif phase == Phase.PACKAGE_SKIPPED:
                if pkg not in completed and pkg not in skipped:
                    skipped.append(pkg)
            elif phase == Phase.PACKAGE_FAILED:
                if pkg in completed:
                    completed.remove(pkg)
                if pkg not in failed:
                    failed.append(pkg)

        return completed, skipped, failed

    def _state_from_group(self, queue_id, group, start):
        """Builds a ResumeState from one queue's events.

        Args:
            queue_id (str): The queue identifier.
            group (list): Events belonging to the queue, in order.
            start (dict): The queue_started event.

        Returns:
            ResumeState: The reconstructed state.

        """
        completed, skipped, failed = self._package_outcomes(group)

        in_progress = None
        for event in group:
            phase = event['event']
            pkg = event.get('package')
            if phase == Phase.PACKAGE_STARTED:
                in_progress = pkg
            elif phase in (Phase.PACKAGE_COMPLETED, Phase.PACKAGE_SKIPPED,
                           Phase.PACKAGE_FAILED) and pkg == in_progress:
                in_progress = None

        return ResumeState(
            queue_id=queue_id,
            target=start.get('target'),
            queue=list(start.get('queue', [])),
            completed=completed,
            skipped=skipped,
            in_progress=in_progress,
            failed=failed[-1] if failed else None,
            started_at=start.get('ts'),
            last_event_at=group[-1].get('ts'),
        )

    def _summarise(self, queue_id, group, start):
        """Builds a BuildSummary from one queue's events.

        Args:
            queue_id (str): The queue identifier.
            group (list): Events belonging to the queue, in order.
            start (dict): The queue_started event.

        Returns:
            BuildSummary: The summarised build.

        """
        completed, skipped, failed = self._package_outcomes(group)
        terminal = next((event for event in reversed(group)
                         if event['event'] in TERMINAL_QUEUE_PHASES), None)

        if terminal is None:
            outcome = OUTCOME_UNFINISHED
            finished_at = None
            reason = None
        elif terminal['event'] == Phase.QUEUE_ABORTED:
            outcome = OUTCOME_ABORTED
            finished_at = terminal.get('ts')
            reason = terminal.get('reason')
        else:
            outcome = terminal.get('status') or (
                OUTCOME_FAILED if failed else OUTCOME_COMPLETED)
            finished_at = terminal.get('ts')
            reason = None

        started = parse_timestamp(start.get('ts'))
        finished = parse_timestamp(finished_at)
        duration = None
        if started is not None and finished is not None:
            duration = (finished - started).total_seconds()

        return BuildSummary(
            queue_id=queue_id,
            target=start.get('target'),
            outcome=outcome,
            queued=len(start.get('queue', [])),
            completed=completed,
            skipped=skipped,
            failed=failed,
            started_at=start.get('ts'),
            finished_at=finished_at,
            duration_seconds=duration,
            reason=reason,
        )

    def _maybe_rotate(self):
        """Rotates the journal if it has outgrown max_bytes.

        Returns:
            bool: True if a rotation happened.

        """
        if self.max_bytes <= 0:
            return False
        try:
            size = self.path.stat().st_size
        except OSError:
            return False
        if size < self.max_bytes:
            return False

        # Read the unfinished queue before the file moves out from under us:
        # rotation must never be the reason a build cannot be resumed.
        groups = self._queue_groups(self.iter_events())
        carried = self._carryover_events(groups)

        if not self._rotate_files():
            return False

        if carried:
            self._rotating = True
            try:
                for event in carried:
                    self._write_event(event)
            finally:
                self._rotating = False
        return True

    def _carryover_events(self, groups):
        """Returns the events that must be copied into the rotated journal.

        Only the events --resume depends on are carried, so a long build
        cannot defeat rotation by growing its own carry-over without bound.

        Args:
            groups (dict): queue_id -> events, as returned by _queue_groups().

        Returns:
            list: Events to re-write, in their original order.

        """
        for queue_id in reversed(list(groups)):
            group = groups[queue_id]
            if any(event['event'] in TERMINAL_QUEUE_PHASES for event in group):
                continue
            start = next(
                (e for e in group if e['event'] == Phase.QUEUE_STARTED), None)
            if start is None:
                continue

            state = self._state_from_group(queue_id, group, start)
            tail = {pkg for pkg in (state.in_progress, state.failed) if pkg}
            carried = []
            for event in group:
                phase = event['event']
                if phase in RESUME_PHASES or (
                        phase in (Phase.PACKAGE_STARTED, Phase.PACKAGE_FAILED)
                        and event.get('package') in tail):
                    carried.append(dict(event, carried=True))
            return carried

        return []

    def _rotate_files(self):
        """Shifts the journal and its backups one slot along.

        Returns:
            bool: True if the active journal was moved or removed.

        """
        try:
            if self.backup_count == 0:
                self.path.unlink()
                return True

            for index in range(self.backup_count - 1, 0, -1):
                source = Path(f'{self.path}.{index}')
                if source.is_file():
                    os.replace(source, Path(f'{self.path}.{index + 1}'))
            os.replace(self.path, Path(f'{self.path}.1'))
            return True
        except OSError as exc:
            logging.error(colored(
                f'Could not rotate build journal {self.path}: {exc}', 'red'))
            return False
