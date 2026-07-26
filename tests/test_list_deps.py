"""Dependency resolution tests.

The pre-1.0.8 resolver mutated the list it was iterating over, so a package
re-encountered as a shared dependency shifted the cursor and silently skipped
the next entry. Across BLFS 11.3 that lost 1286 required-dependency edges in
168 of 924 packages. These tests pin the corrected behaviour.
"""

import pytest

from blfs_manager.commands import Commands
from tests.conftest import entry


def assert_build_order(order, database):
    """Every required dependency must appear before the package needing it."""
    position = {pkg: i for i, pkg in enumerate(order)}
    for pkg in order:
        if pkg not in database:
            continue
        for dep in database[pkg]['deps']['required']:
            if dep in position:
                assert position[dep] < position[pkg], (
                    f'{dep} must be built before {pkg}')


def test_returns_full_transitive_closure(commands, diamond_db):
    order = commands.list_deps('A-1.0')
    assert set(order) == {'A-1.0', 'B-1.0', 'C-1.0', 'D-1.0'}


def test_shared_dependency_is_not_dropped(commands, diamond_db):
    # D is reached through both B and C; the old resolver lost it.
    assert 'D-1.0' in commands.list_deps('A-1.0')


def test_requested_package_is_built_last(commands):
    assert commands.list_deps('A-1.0')[-1] == 'A-1.0'


def test_dependencies_precede_dependents(commands, diamond_db):
    assert_build_order(commands.list_deps('A-1.0'), diamond_db)


def test_no_duplicates_in_build_order(commands):
    order = commands.list_deps('A-1.0')
    assert len(order) == len(set(order))


def test_deep_chain_is_fully_expanded():
    db = {f'P{i}': entry(f'P{i}', required=[f'P{i + 1}']) for i in range(50)}
    db['P50'] = entry('P50')
    order = Commands(db, []).list_deps('P0')
    assert len(order) == 51
    assert order[0] == 'P50' and order[-1] == 'P0'


def test_circular_dependency_terminates():
    # Shadow <-> Linux-PAM is a real BLFS cycle; resolution must not hang.
    db = {
        'Shadow': entry('Shadow', required=['Linux-PAM']),
        'Linux-PAM': entry('Linux-PAM', required=['Shadow']),
    }
    order = Commands(db, []).list_deps('Shadow')
    assert set(order) == {'Shadow', 'Linux-PAM'}
    assert order[-1] == 'Shadow'


def test_self_dependency_terminates():
    db = {'X': entry('X', required=['X'])}
    assert Commands(db, []).list_deps('X') == ['X']


def test_unknown_dependency_is_still_listed():
    # Book cross-references ("Setting up the Xorg Build Environment") are not
    # packages, but the user still needs to see them.
    db = {'A': entry('A', required=['Not In Book'])}
    order = Commands(db, []).list_deps('A')
    assert order == ['Not In Book', 'A']


def test_recommended_included_only_with_flag():
    db = {
        'A': entry('A', required=['B'], recommended=['R']),
        'B': entry('B'), 'R': entry('R'),
    }
    action = Commands(db, [])
    assert 'R' not in action.list_deps('A')
    assert 'R' in action.list_deps('A', rec=True)


def test_optional_flag_implies_recommended():
    db = {
        'A': entry('A', recommended=['R'], optional=['O']),
        'R': entry('R'), 'O': entry('O'),
    }
    order = Commands(db, []).list_deps('A', opt=True)
    assert {'R', 'O'} <= set(order)


def test_recommended_and_optional_together():
    # Previously an `elif` meant -r silently suppressed -o.
    db = {
        'A': entry('A', recommended=['R'], optional=['O']),
        'R': entry('R'), 'O': entry('O'),
    }
    order = Commands(db, []).list_deps('A', rec=True, opt=True)
    assert {'R', 'O'} <= set(order)


def test_optional_deps_not_expanded_transitively():
    # Pulling optionals recursively would drag in most of the book.
    db = {
        'A': entry('A', required=['B'], optional=['O']),
        'B': entry('B', optional=['Huge']),
        'O': entry('O'), 'Huge': entry('Huge'),
    }
    order = Commands(db, []).list_deps('A', opt=True)
    assert 'O' in order and 'Huge' not in order


def test_missing_package_exits():
    with pytest.raises(SystemExit):
        Commands({'A': entry('A')}, []).list_deps('nonexistent-pkg')
