import pytest

from blfs_manager.commands import Commands


def entry(name, required=(), recommended=(), optional=(),
          urls=(), hashes=(), commands=(), kconf=(), pkg_type='BLFS'):
    """Builds a database entry in the same shape bootstrapper.DbEntry produces."""
    return {
        'name': name,
        'url': list(urls),
        'deps': {
            'required': list(required),
            'recommended': list(recommended),
            'optional': list(optional),
        },
        'commands': list(commands),
        'hashes': list(hashes),
        'kconf': list(kconf),
        'pkg_type': pkg_type,
    }


@pytest.fixture
def diamond_db():
    """A -> B, C ; B -> D ; C -> D. The shape the old resolver silently broke."""
    return {
        'A-1.0': entry('A-1.0', required=['B-1.0', 'C-1.0']),
        'B-1.0': entry('B-1.0', required=['D-1.0']),
        'C-1.0': entry('C-1.0', required=['D-1.0']),
        'D-1.0': entry('D-1.0'),
    }


@pytest.fixture
def commands(diamond_db):
    return Commands(diamond_db, [])
