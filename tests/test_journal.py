"""Tests for the crash-safe build journal and --resume support."""

import json
import os
import signal
import threading
from datetime import datetime, timedelta, timezone

import pytest

from blfs_manager.journal import (
    BuildJournal, OUTCOME_ABORTED, OUTCOME_COMPLETED, OUTCOME_FAILED,
    OUTCOME_UNFINISHED, Phase, SCHEMA_VERSION, parse_timestamp,
)

QUEUE = ['D-1.0', 'B-1.0', 'C-1.0', 'A-1.0']

# Padding wide enough that a couple of events cross the rotation thresholds
# used below, so rotation tests stay fast (every event is fsync'd).
PAD = 400


@pytest.fixture
def journal_path(tmp_path):
    return tmp_path / 'state' / 'build_journal.jsonl'


@pytest.fixture
def journal(journal_path):
    return BuildJournal(journal_path)


def lines(path):
    """Returns the raw lines of a journal file."""
    return path.read_text().splitlines()


def truncate_last_line(path, keep=10):
    """Chops the final line in half, the way a power cut would."""
    text = path.read_text()
    body, _, tail = text.rstrip('\n').rpartition('\n')
    path.write_text(f'{body}\n{tail[:keep]}')


def build_through(journal, upto, queue=QUEUE, target='A-1.0'):
    """Starts a queue and completes packages up to (excluding) upto.

    Args:
        journal (BuildJournal): The journal to write to.
        upto (int): Index in queue at which the build is interrupted.
        queue (list): The resolved build order.
        target (str): The requested package.

    Returns:
        str: The queue identifier.

    """
    queue_id = journal.start_queue(target, queue)
    for pkg in queue[:upto]:
        journal.start_package(pkg)
        journal.record_command(pkg, 'make install', 0)
        journal.complete_package(pkg)
    return queue_id


class TestEventRecording:
    def test_queue_start_records_the_full_queue(self, journal, journal_path):
        queue_id = journal.start_queue('A-1.0', QUEUE)

        events = journal.read_events()
        assert len(events) == 1
        assert events[0]['event'] == Phase.QUEUE_STARTED
        assert events[0]['queue'] == QUEUE
        assert events[0]['target'] == 'A-1.0'
        assert events[0]['queue_id'] == queue_id
        assert events[0]['v'] == SCHEMA_VERSION

    def test_every_event_is_one_line_of_json(self, journal, journal_path):
        build_through(journal, 2)
        journal.complete_queue()

        raw = lines(journal_path)
        assert len(raw) == journal_path.read_text().count('\n')
        for line in raw:
            assert isinstance(json.loads(line), dict)

    def test_all_phases_round_trip(self, journal):
        journal.start_queue('A-1.0', QUEUE)
        journal.start_package('D-1.0')
        journal.record_extracted('D-1.0', '/src/D-1.0')
        journal.record_command('D-1.0', './configure --prefix=/usr', 0)
        journal.complete_package('D-1.0')
        journal.skip_package('B-1.0', reason='already installed')
        journal.start_package('C-1.0')
        journal.record_command('C-1.0', 'make', 2)
        journal.fail_package('C-1.0', status=2, reason='make failed')
        journal.abort_queue(reason='build failed')

        phases = [event['event'] for event in journal.read_events()]
        assert phases == [
            Phase.QUEUE_STARTED, Phase.PACKAGE_STARTED,
            Phase.PACKAGE_EXTRACTED, Phase.COMMAND_RUN,
            Phase.PACKAGE_COMPLETED, Phase.PACKAGE_SKIPPED,
            Phase.PACKAGE_STARTED, Phase.COMMAND_RUN,
            Phase.PACKAGE_FAILED, Phase.QUEUE_ABORTED,
        ]

    def test_command_events_carry_command_and_exit_status(self, journal):
        journal.start_queue('A-1.0', QUEUE)
        journal.record_command('D-1.0', 'make -j4 install', 127)

        event = journal.read_events()[-1]
        assert event['command'] == 'make -j4 install'
        assert event['status'] == 127
        assert event['package'] == 'D-1.0'

    def test_timestamps_are_timezone_aware_utc(self, journal):
        journal.start_queue('A-1.0', QUEUE)

        stamp = parse_timestamp(journal.read_events()[0]['ts'])
        assert stamp is not None
        assert stamp.tzinfo is not None
        assert stamp.utcoffset() == timedelta(0)

    def test_parent_directories_are_created(self, journal, journal_path):
        assert not journal_path.parent.exists()
        journal.start_queue('A-1.0', QUEUE)
        assert journal_path.is_file()

    def test_a_second_instance_reads_what_the_first_wrote(self, journal_path):
        first = BuildJournal(journal_path)
        build_through(first, 2)

        second = BuildJournal(journal_path)
        assert len(second.read_events()) == len(first.read_events())

    def test_events_without_an_active_queue_carry_no_queue_id(self, journal):
        journal.complete_package('D-1.0')
        assert 'queue_id' not in journal.read_events()[0]

    def test_complete_queue_without_a_queue_is_a_no_op(self, journal):
        assert journal.complete_queue() is None
        assert journal.abort_queue() is None
        assert journal.read_events() == []


class TestDamagedJournal:
    def test_truncated_final_line_is_tolerated(self, journal, journal_path):
        build_through(journal, 3)
        expected = len(journal.read_events()) - 1
        truncate_last_line(journal_path)

        events = journal.read_events()
        assert len(events) == expected
        assert all(isinstance(event, dict) for event in events)

    def test_truncated_final_line_still_resumes(self, journal, journal_path):
        build_through(journal, 2)
        journal.start_package('C-1.0')
        truncate_last_line(journal_path)

        state = journal.resume_state()
        assert state is not None
        assert state.completed == ['D-1.0', 'B-1.0']
        assert state.remaining == ['C-1.0', 'A-1.0']

    def test_garbage_line_in_the_middle_is_skipped(self, journal, journal_path):
        build_through(journal, 2)
        with open(journal_path, 'a') as handle:
            handle.write('{"event": "package_completed"\n')
        journal.complete_package('C-1.0')

        state = journal.resume_state()
        assert state.completed == ['D-1.0', 'B-1.0', 'C-1.0']

    def test_non_object_line_is_skipped(self, journal, journal_path):
        journal.start_queue('A-1.0', QUEUE)
        with open(journal_path, 'a') as handle:
            handle.write('"just a string"\n[]\n\n')

        assert len(journal.read_events()) == 1

    def test_absent_journal_reads_cleanly(self, journal):
        assert not journal.exists()
        assert journal.read_events() == []
        assert journal.resume_state() is None
        assert journal.history() == []

    def test_empty_journal_reads_cleanly(self, journal, journal_path):
        journal_path.parent.mkdir(parents=True)
        journal_path.write_text('')

        assert journal.read_events() == []
        assert journal.resume_state() is None

    def test_queue_with_lost_start_event_is_not_resumable(self, journal, journal_path):
        build_through(journal, 2)
        surviving = [line for line in lines(journal_path)
                     if json.loads(line)['event'] != Phase.QUEUE_STARTED]
        journal_path.write_text('\n'.join(surviving) + '\n')

        assert journal.resume_state() is None


class TestResume:
    def test_crash_mid_package_resumes_at_that_package(self, journal_path):
        journal = BuildJournal(journal_path)
        build_through(journal, 2)
        journal.start_package('C-1.0')
        journal.record_command('C-1.0', './configure', 0)

        state = BuildJournal(journal_path).resume_state()
        assert state.in_progress == 'C-1.0'
        assert state.resume_point == 'C-1.0'
        assert state.remaining == ['C-1.0', 'A-1.0']

    def test_completed_packages_are_excluded_from_the_remaining_queue(self, journal):
        build_through(journal, 3)

        state = journal.resume_state()
        assert state.completed == ['D-1.0', 'B-1.0', 'C-1.0']
        assert state.remaining == ['A-1.0']

    def test_skipped_packages_are_excluded_from_the_remaining_queue(self, journal):
        journal.start_queue('A-1.0', QUEUE)
        journal.skip_package('D-1.0', reason='already installed')
        journal.skip_package('B-1.0', reason='book section')

        state = journal.resume_state()
        assert state.skipped == ['D-1.0', 'B-1.0']
        assert state.remaining == ['C-1.0', 'A-1.0']

    def test_failed_package_is_retried_first(self, journal):
        build_through(journal, 2)
        journal.start_package('C-1.0')
        journal.fail_package('C-1.0', status=1, reason='make failed')

        state = journal.resume_state()
        assert state.failed == 'C-1.0'
        assert state.in_progress is None
        assert state.resume_point == 'C-1.0'

    def test_remaining_preserves_the_resolved_build_order(self, journal):
        journal.start_queue('A-1.0', QUEUE)
        journal.complete_package('C-1.0')

        assert journal.resume_state().remaining == ['D-1.0', 'B-1.0', 'A-1.0']

    def test_fully_completed_queue_has_nothing_to_resume(self, journal):
        build_through(journal, len(QUEUE))
        journal.complete_queue()

        assert journal.resume_state() is None

    def test_queue_completed_without_building_everything_is_not_resumable(self, journal):
        build_through(journal, 2)
        journal.complete_queue()

        assert journal.resume_state() is None

    def test_aborted_queue_is_not_resumable(self, journal):
        build_through(journal, 2)
        journal.abort_queue(reason='interrupted')

        assert journal.resume_state() is None

    def test_the_most_recent_unfinished_queue_wins(self, journal):
        build_through(journal, 4)
        journal.complete_queue()
        build_through(journal, 1, queue=['X-1.0', 'Y-1.0'], target='Y-1.0')

        state = journal.resume_state()
        assert state.target == 'Y-1.0'
        assert state.remaining == ['Y-1.0']

    def test_resume_queue_reattaches_to_the_same_queue(self, journal_path):
        first = BuildJournal(journal_path)
        queue_id = build_through(first, 2)

        second = BuildJournal(journal_path)
        state = second.resume_queue()
        assert state.queue_id == queue_id
        assert second.active_queue_id == queue_id

        for pkg in state.remaining:
            second.start_package(pkg)
            second.complete_package(pkg)
        second.complete_queue()

        assert second.resume_state() is None
        assert len(second.history()) == 1
        assert second.history()[0].outcome == OUTCOME_COMPLETED

    def test_resume_queue_records_a_resume_event(self, journal):
        build_through(journal, 2)
        journal.resume_queue()

        event = journal.read_events()[-1]
        assert event['event'] == Phase.QUEUE_RESUMED
        assert event['remaining'] == 2

    def test_resume_queue_returns_none_when_nothing_is_pending(self, journal):
        build_through(journal, 4)
        journal.complete_queue()

        assert journal.resume_queue() is None
        assert journal.read_events()[-1]['event'] == Phase.QUEUE_COMPLETED

    @pytest.mark.skipif(not hasattr(os, 'fork'), reason='needs fork()')
    def test_sigkill_mid_package_leaves_a_resumable_journal(self, journal_path):
        # The whole point of the fsync: a build that is killed outright, with
        # no chance to flush or clean up, must still be resumable.
        pid = os.fork()
        if pid == 0:
            try:
                child = BuildJournal(journal_path)
                child.start_queue('A-1.0', QUEUE)
                child.complete_package('D-1.0')
                child.start_package('B-1.0')
            finally:
                os.kill(os.getpid(), signal.SIGKILL)

        _, status = os.waitpid(pid, 0)
        assert os.WIFSIGNALED(status)

        state = BuildJournal(journal_path).resume_state()
        assert state.completed == ['D-1.0']
        assert state.in_progress == 'B-1.0'
        assert state.remaining == ['B-1.0', 'C-1.0', 'A-1.0']

    def test_retried_package_is_no_longer_failed(self, journal):
        journal.start_queue('A-1.0', QUEUE)
        journal.fail_package('D-1.0', status=1)
        journal.complete_package('D-1.0')

        state = journal.resume_state()
        assert state.failed is None
        assert state.completed == ['D-1.0']


class TestRotation:
    def _fill(self, journal, count=6, queue=('X-1.0',), target='X-1.0'):
        journal.start_queue(target, list(queue))
        for index in range(count):
            journal.record_command('X-1.0', 'echo ' + 'x' * PAD, index)

    def test_rotation_creates_a_backup(self, journal_path):
        journal = BuildJournal(journal_path, max_bytes=512)
        self._fill(journal)

        assert journal_path.with_suffix('.jsonl.1').is_file()
        assert journal_path.stat().st_size < 512 * 4

    def test_rotation_is_disabled_when_max_bytes_is_zero(self, journal_path):
        journal = BuildJournal(journal_path, max_bytes=0)
        self._fill(journal)

        assert not journal_path.with_suffix('.jsonl.1').exists()

    def test_rotation_preserves_the_active_queue(self, journal_path):
        journal = BuildJournal(journal_path, max_bytes=512)
        journal.start_queue('A-1.0', QUEUE)
        journal.complete_package('D-1.0')
        journal.skip_package('B-1.0', reason='already installed')
        journal.start_package('C-1.0')
        for index in range(6):
            journal.record_command('C-1.0', 'echo ' + 'y' * PAD, 0)

        assert journal_path.with_suffix('.jsonl.1').is_file()
        state = BuildJournal(journal_path).resume_state()
        assert state is not None
        assert state.completed == ['D-1.0']
        assert state.skipped == ['B-1.0']
        assert state.in_progress == 'C-1.0'
        assert state.remaining == ['C-1.0', 'A-1.0']

    def test_carried_events_are_flagged(self, journal_path):
        journal = BuildJournal(journal_path, max_bytes=512)
        journal.start_queue('A-1.0', QUEUE)
        journal.complete_package('D-1.0')
        for index in range(6):
            journal.record_command('D-1.0', 'echo ' + 'z' * PAD, 0)

        carried = [event for event in journal.read_events()
                   if event.get('carried')]
        assert {event['event'] for event in carried} == {
            Phase.QUEUE_STARTED, Phase.PACKAGE_COMPLETED}

    def test_rotation_drops_noise_but_keeps_resume_events(self, journal_path):
        journal = BuildJournal(journal_path, max_bytes=512)
        journal.start_queue('A-1.0', QUEUE)
        journal.complete_package('D-1.0')
        for index in range(6):
            journal.record_command('D-1.0', 'echo ' + 'z' * PAD, 0)

        phases = [event['event'] for event in journal.read_events()]
        assert phases.count(Phase.QUEUE_STARTED) == 1
        assert phases.count(Phase.PACKAGE_COMPLETED) == 1

    def test_finished_queue_is_not_carried_forward(self, journal_path):
        journal = BuildJournal(journal_path, max_bytes=512)
        self._fill(journal)
        journal.complete_queue()
        self._fill(journal, 1, queue=('Z-1.0',), target='Z-1.0')

        targets = [event.get('target') for event in journal.read_events()
                   if event['event'] == Phase.QUEUE_STARTED]
        assert 'Z-1.0' in targets

    def test_backup_count_is_respected(self, journal_path):
        journal = BuildJournal(journal_path, max_bytes=512, backup_count=2)
        for round_index in range(6):
            self._fill(journal, 3)
            journal.complete_queue()

        assert journal_path.with_suffix('.jsonl.1').is_file()
        assert journal_path.with_suffix('.jsonl.2').is_file()
        assert not journal_path.with_suffix('.jsonl.3').exists()

    def test_zero_backups_discards_the_old_journal(self, journal_path):
        journal = BuildJournal(journal_path, max_bytes=512, backup_count=0)
        self._fill(journal)

        assert not journal_path.with_suffix('.jsonl.1').exists()
        assert journal_path.is_file()

    def test_rotated_events_are_readable_on_request(self, journal_path):
        journal = BuildJournal(journal_path, max_bytes=512)
        self._fill(journal)
        journal.complete_queue()

        assert len(journal.read_events(include_rotated=True)) > \
            len(journal.read_events())


class TestHistory:
    def test_completed_build_is_summarised(self, journal):
        build_through(journal, 4)
        journal.complete_queue()

        summary = journal.history()[0]
        assert summary.target == 'A-1.0'
        assert summary.outcome == OUTCOME_COMPLETED
        assert summary.queued == 4
        assert summary.completed == QUEUE
        assert summary.failed == []
        assert summary.duration_seconds is not None
        assert summary.duration_seconds >= 0

    def test_failed_build_lists_the_failures(self, journal):
        build_through(journal, 2)
        journal.start_package('C-1.0')
        journal.fail_package('C-1.0', status=1, reason='make failed')
        journal.complete_queue()

        summary = journal.history()[0]
        assert summary.outcome == OUTCOME_FAILED
        assert summary.failed == ['C-1.0']
        assert summary.completed == ['D-1.0', 'B-1.0']

    def test_aborted_build_keeps_its_reason(self, journal):
        build_through(journal, 1)
        journal.abort_queue(reason='interrupted by user')

        summary = journal.history()[0]
        assert summary.outcome == OUTCOME_ABORTED
        assert summary.reason == 'interrupted by user'

    def test_unfinished_build_is_reported_as_such(self, journal):
        build_through(journal, 1)

        summary = journal.history()[0]
        assert summary.outcome == OUTCOME_UNFINISHED
        assert summary.finished_at is None
        assert summary.duration_seconds is None

    def test_history_is_newest_first_and_limited(self, journal):
        for index in range(4):
            build_through(journal, 1, queue=[f'P{index}-1.0'],
                          target=f'P{index}-1.0')
            journal.complete_queue()

        summaries = journal.history(limit=2)
        assert [summary.target for summary in summaries] == ['P3-1.0', 'P2-1.0']

    def test_history_limit_none_returns_everything(self, journal):
        for index in range(3):
            build_through(journal, 1, queue=[f'P{index}-1.0'],
                          target=f'P{index}-1.0')
            journal.complete_queue()

        assert len(journal.history(limit=None)) == 3

    def test_history_can_include_rotated_journals(self, journal_path):
        journal = BuildJournal(journal_path, max_bytes=512)
        for index in range(3):
            journal.start_queue(f'P{index}-1.0', [f'P{index}-1.0'])
            for _ in range(3):
                journal.record_command(f'P{index}-1.0', 'echo ' + 'q' * PAD, 0)
            journal.complete_package(f'P{index}-1.0')
            journal.complete_queue()

        assert len(journal.history(limit=None, include_rotated=True)) >= \
            len(journal.history(limit=None))

    def test_print_history_handles_an_empty_journal(self, journal):
        assert journal.print_history() == []

    def test_print_history_reports_each_build(self, journal):
        build_through(journal, 2)
        journal.complete_queue()

        assert len(journal.print_history()) == 1


class TestConcurrentAppends:
    def test_parallel_writers_never_interleave_a_line(self, journal_path):
        writers = 4
        per_writer = 30

        def write(index):
            journal = BuildJournal(journal_path)
            journal.start_queue(f'P{index}-1.0', [f'P{index}-1.0'])
            for step in range(per_writer):
                journal.record_command(
                    f'P{index}-1.0', f'make step-{step} ' + 'w' * 100, 0)

        threads = [threading.Thread(target=write, args=(index,))
                   for index in range(writers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        raw = lines(journal_path)
        assert len(raw) == writers * (per_writer + 1)
        for line in raw:
            assert isinstance(json.loads(line), dict)


class TestClear:
    def test_clear_removes_journal_and_backups(self, journal_path):
        journal = BuildJournal(journal_path, max_bytes=512, backup_count=2)
        for _ in range(4):
            journal.start_queue('X-1.0', ['X-1.0'])
            for index in range(3):
                journal.record_command('X-1.0', 'echo ' + 'x' * PAD, 0)
            journal.complete_queue()

        journal.clear()
        assert not journal_path.exists()
        assert not journal_path.with_suffix('.jsonl.1').exists()
        assert journal.read_events(include_rotated=True) == []

    def test_clear_on_a_missing_journal_is_silent(self, journal):
        journal.clear()
        assert not journal.exists()


class TestParseTimestamp:
    def test_parses_a_journal_timestamp(self):
        stamp = datetime.now(timezone.utc).isoformat()
        assert parse_timestamp(stamp).tzinfo is not None

    def test_rejects_junk(self):
        assert parse_timestamp('not a time') is None
        assert parse_timestamp(None) is None
