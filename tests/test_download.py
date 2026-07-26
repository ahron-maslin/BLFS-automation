"""Tests for the download pipeline.

The pre-1.0.8 loop paired URLs with hashes using zip(). The book records one
MD5 sum per package but lists patches as extra URLs, so for 89 of 924 BLFS
packages zip() truncated the list and the patch was never downloaded -- the
build then failed at `patch -Np1`.
"""

import hashlib

import pytest

from blfs_manager import commands as commands_module
from blfs_manager.commands import Commands
from tests.conftest import entry

TARBALL = 'https://example.invalid/pkg-1.0.tar.xz'
PATCH = 'https://www.linuxfromscratch.org/patches/blfs/11.3/pkg-1.0-fix-1.patch'


@pytest.fixture
def fake_wget(monkeypatch, tmp_path):
    """Records download attempts and writes a stub file for each."""
    calls = []

    def download(url, filename):
        calls.append(url)
        with open(filename, 'wb') as handle:
            handle.write(b'stub')
        return filename

    monkeypatch.setattr(commands_module.wget, 'download', download)
    monkeypatch.setattr(commands_module, 'check_dir', lambda: None)
    monkeypatch.chdir(tmp_path)
    return calls


STUB_MD5 = hashlib.md5(b'stub').hexdigest()


def test_patch_url_is_downloaded_despite_single_hash(fake_wget):
    db = {'pkg': entry('pkg', urls=[TARBALL, PATCH], hashes=[STUB_MD5])}
    Commands(db, []).download_deps(['pkg'])
    assert fake_wget == [TARBALL, PATCH], 'patch URL must not be dropped'


def test_all_urls_downloaded_when_hashes_absent(fake_wget):
    db = {'pkg': entry('pkg', urls=[TARBALL, PATCH], hashes=[])}
    Commands(db, []).download_deps(['pkg'])
    assert fake_wget == [TARBALL, PATCH]


def test_checksum_mismatch_raises_and_deletes(fake_wget, tmp_path):
    db = {'pkg': entry('pkg', urls=[TARBALL], hashes=['0' * 32])}
    with pytest.raises(OSError, match='does not match'):
        Commands(db, []).download_deps(['pkg'])
    assert not (tmp_path / 'pkg-1.0.tar.xz').exists()


def test_existing_file_is_not_redownloaded(fake_wget, tmp_path):
    (tmp_path / 'pkg-1.0.tar.xz').write_bytes(b'already here')
    db = {'pkg': entry('pkg', urls=[TARBALL], hashes=[STUB_MD5])}
    Commands(db, []).download_deps(['pkg'])
    assert fake_wget == []


def test_non_archive_urls_are_skipped(fake_wget):
    db = {'pkg': entry('pkg', urls=['https://example.invalid/homepage.html'],
                       hashes=[])}
    Commands(db, []).download_deps(['pkg'])
    assert fake_wget == []


def test_failed_download_removes_partial_file(monkeypatch, tmp_path):
    def failing(url, filename):
        with open(filename, 'wb') as handle:
            handle.write(b'partial')
        raise OSError('connection reset')

    monkeypatch.setattr(commands_module.wget, 'download', failing)
    monkeypatch.setattr(commands_module, 'check_dir', lambda: None)
    monkeypatch.chdir(tmp_path)

    db = {'pkg': entry('pkg', urls=[TARBALL], hashes=[STUB_MD5])}
    Commands(db, []).download_deps(['pkg'])
    assert not (tmp_path / 'pkg-1.0.tar.xz').exists(), (
        'a truncated download must not be mistaken for a complete one')


def test_book_section_is_skipped_not_downloaded(fake_wget):
    db = {'Xorg Libraries': entry('Xorg Libraries', urls=[TARBALL], hashes=[])}
    Commands(db, []).download_deps(['Xorg Libraries'])
    assert fake_wget == []


def test_unknown_package_does_not_crash(fake_wget):
    Commands({}, []).download_deps(['Cantarell fonts'])
    assert fake_wget == []
