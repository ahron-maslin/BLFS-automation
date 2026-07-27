"""Tests that Commands actually drives the build journal correctly.

blfs_manager.journal is unit-tested in isolation in tests/test_journal.py.
These tests cover the wiring in commands.py: that build_pkg/install_package
record the right events at the right points, and that --resume (build_pkg
called with resume=True) picks up where an interrupted queue left off.

Extraction and command execution are stubbed -- they are covered by
tests/test_utils.py and the extraction tests in test_list_deps.py -- so these
tests isolate the journal wiring itself.
"""

import pytest

from blfs_manager.commands import Commands
from blfs_manager.journal import BuildJournal
from tests.conftest import entry


@pytest.fixture
def journal(tmp_path):
    return BuildJournal(tmp_path / 'build.jsonl')


@pytest.fixture
def db():
    return {
        'A-1.0': entry('A-1.0', required=['B-1.0']),
        'B-1.0': entry('B-1.0'),
    }


def stub_successful_build(monkeypatch):
    monkeypatch.setattr(Commands, '_extract_source', lambda self, pkg: True)
    monkeypatch.setattr(Commands, '_run_install_commands', lambda self, pkg: True)
    monkeypatch.setattr(Commands, 'download_deps', lambda self, dlist: None)
    monkeypatch.setattr(Commands, 'write_installed_log', lambda self: None)
    monkeypatch.setattr('os.chdir', lambda path: None)
    monkeypatch.setattr('os.getcwd', lambda: '/fake/build/dir')
    monkeypatch.setattr(
        'blfs_manager.commands.rmtree', lambda path, ignore_errors=False: None)


def test_build_pkg_starts_and_completes_a_queue(db, journal, monkeypatch):
    stub_successful_build(monkeypatch)
    action = Commands(db, [], journal=journal)

    action.build_pkg('A-1.0')

    history = journal.history()
    assert len(history) == 1
    assert history[0].target == 'A-1.0'
    assert history[0].completed == ['B-1.0', 'A-1.0']


def test_install_package_records_completion(db, journal, monkeypatch):
    stub_successful_build(monkeypatch)
    action = Commands(db, [], journal=journal)
    journal.start_queue('B-1.0', ['B-1.0'])

    action.install_package('B-1.0', force=False)

    state = journal.resume_state()
    assert state.remaining == [], 'a completed package must not appear as remaining'


def test_already_installed_package_is_recorded_as_skipped(db, journal, monkeypatch):
    stub_successful_build(monkeypatch)
    action = Commands(db, ['B-1.0'], journal=journal)
    journal.start_queue('A-1.0', ['B-1.0', 'A-1.0'])

    action.install_package('B-1.0', force=False)

    state = journal.resume_state()
    assert 'B-1.0' in state.skipped
    assert 'B-1.0' not in state.remaining


def test_book_section_is_recorded_as_skipped(journal, monkeypatch):
    stub_successful_build(monkeypatch)
    db = {'Xorg Libraries': entry('Xorg Libraries')}
    action = Commands(db, [], journal=journal)
    journal.start_queue('Xorg Libraries', ['Xorg Libraries'])

    action.install_package('Xorg Libraries', force=False)

    state = journal.resume_state()
    assert 'Xorg Libraries' in state.skipped


def test_extraction_failure_is_recorded_as_failed(db, journal, monkeypatch):
    stub_successful_build(monkeypatch)
    monkeypatch.setattr(Commands, '_extract_source', lambda self, pkg: False)
    action = Commands(db, [], journal=journal)
    journal.start_queue('B-1.0', ['B-1.0'])

    action.install_package('B-1.0', force=False)

    state = journal.resume_state()
    assert state is not None
    assert state.failed == 'B-1.0'
    assert 'B-1.0' in state.remaining, 'a failed package must still be resumable'


def test_failed_build_command_is_recorded_as_failed(db, journal, monkeypatch):
    stub_successful_build(monkeypatch)
    monkeypatch.setattr(Commands, '_run_install_commands', lambda self, pkg: False)
    action = Commands(db, [], journal=journal)
    journal.start_queue('B-1.0', ['B-1.0'])

    action.install_package('B-1.0', force=False)

    state = journal.resume_state()
    assert state.failed == 'B-1.0'


def test_resume_continues_from_the_failure(db, journal, monkeypatch):
    stub_successful_build(monkeypatch)
    action = Commands(db, [], journal=journal)

    journal.start_queue('A-1.0', ['B-1.0', 'A-1.0'])
    journal.start_package('B-1.0')
    journal.complete_package('B-1.0')

    action.build_pkg(None, resume=True)

    history = journal.history()
    assert history[0].completed == ['B-1.0', 'A-1.0']


def test_resume_with_nothing_to_resume_does_not_crash(db, journal, monkeypatch):
    stub_successful_build(monkeypatch)
    action = Commands(db, [], journal=journal)

    action.build_pkg(None, resume=True)

    assert journal.history() == []


def test_sigint_cleanup_aborts_the_active_queue(db, journal, monkeypatch, tmp_path):
    monkeypatch.setattr('os.chdir', lambda path: None)
    monkeypatch.setattr('os.path.exists', lambda path: False)
    monkeypatch.setattr(Commands, 'write_installed_log', lambda self: None)
    action = Commands(db, [], journal=journal)
    journal.start_queue('A-1.0', ['B-1.0', 'A-1.0'])
    journal.start_package('B-1.0')

    with pytest.raises(SystemExit):
        action.cleanup(signum=2, frame=None)

    history = journal.history()
    assert history[0].outcome == 'aborted'
