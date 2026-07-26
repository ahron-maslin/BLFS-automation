"""Regression tests against the real BLFS 11.3 database shipped with the project.

These guard the properties that actually matter to someone building a system:
every required dependency is queued, and nothing is scheduled before what it
needs. They are skipped if the database file is absent.
"""

import json

import pytest

from blfs_manager.commands import Commands
from blfs_manager.define import DB_PATH

pytestmark = pytest.mark.skipif(
    not DB_PATH.exists(), reason='book database not present')


@pytest.fixture(scope='module')
def book():
    with open(DB_PATH) as handle:
        return json.load(handle)


@pytest.fixture(scope='module')
def action(book):
    return Commands(book, [])


def required_closure(book, pkg):
    seen, stack = set(), [pkg]
    while stack:
        current = stack.pop()
        if current in seen or current not in book:
            continue
        seen.add(current)
        stack.extend(book[current]['deps']['required'])
    return seen


def blfs_packages(book):
    return [k for k, v in book.items() if v['pkg_type'] == 'BLFS']


def test_database_is_non_trivial(book):
    assert len(blfs_packages(book)) > 800


def test_every_required_dependency_is_queued(book, action):
    """1286 edges across 168 packages were being lost before this was fixed."""
    incomplete = {}
    for pkg in blfs_packages(book):
        missing = required_closure(book, pkg) - set(action.list_deps(pkg))
        if missing:
            incomplete[pkg] = sorted(missing)
    assert not incomplete, f'{len(incomplete)} packages missing required deps'


def test_build_order_respects_dependencies(book, action):
    """Any inversion must be a genuine cycle in the book, never a resolver bug."""
    for pkg in blfs_packages(book):
        order = action.list_deps(pkg)
        position = {p: i for i, p in enumerate(order)}
        for candidate in order:
            if candidate not in book:
                continue
            for dep in book[candidate]['deps']['required']:
                if dep in position and position[dep] > position[candidate]:
                    assert dep in required_closure(book, candidate) and \
                        candidate in required_closure(book, dep), (
                        f'{dep} scheduled after {candidate} without a cycle')


def test_requested_package_is_always_last(book, action):
    for pkg in blfs_packages(book):
        assert action.list_deps(pkg)[-1] == pkg


def test_build_order_never_repeats_a_package(book, action):
    for pkg in blfs_packages(book):
        order = action.list_deps(pkg)
        assert len(order) == len(set(order)), f'duplicate entries for {pkg}'


# Real circular dependencies in the BLFS book. The removed CIRC_EXCEPTIONS
# constant was meant to special-case the Cups pair but was never referenced by
# any code path (and pinned a stale version). Cycle-breaking now lives in
# list_deps() and is asserted here for every known cycle instead.
KNOWN_CYCLES = [
    ('Cups-2.4.2', 'cups-filters-1.28.16'),
    ('Shadow-4.13', 'Linux-PAM-1.5.2'),
    ('GDM-43.0', 'gnome-shell-43.3'),
    ('Phonon-4.11.1', 'Phonon-backend-gstreamer-4.10.0'),
    ('libnotify-0.8.1', 'xfce4-notifyd-0.8.1'),
]


@pytest.mark.parametrize('left,right', KNOWN_CYCLES)
def test_known_circular_dependencies_terminate(book, action, left, right):
    """Resolution must terminate and still queue both halves of the cycle."""
    if left not in book or right not in book:
        pytest.skip(f'{left}/{right} not in this database revision')

    # Confirm this really is a cycle before asserting we survive it.
    assert right in required_closure(book, left)
    assert left in required_closure(book, right)

    for pkg, partner in ((left, right), (right, left)):
        order = action.list_deps(pkg)
        assert order[-1] == pkg
        assert partner in order, f'{partner} dropped while breaking the cycle'
        assert len(order) == len(set(order))


def test_gtk3_pulls_its_full_stack(book, action):
    """GTK+ used to resolve to 23 packages when it needs 28."""
    if 'GTK+-3.24.36' not in book:
        pytest.skip('GTK+ not in this database revision')
    order = action.list_deps('GTK+-3.24.36')
    for dep in ('libXau-1.0.11', 'xcb-proto-1.15.2', 'xorgproto-2022.2'):
        assert dep in order, f'{dep} silently dropped from the GTK+ build'
