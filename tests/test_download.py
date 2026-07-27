"""Tests for how download_deps turns database entries into download targets.

The pre-1.0.8 loop paired URLs with hashes using zip(). The book records one
MD5 sum per package but lists patches as extra URLs, so for 89 of 924 BLFS
packages zip() truncated the list and the patch was never downloaded -- the
build then failed at `patch -Np1`.

Transport concerns (resume, mirrors, cache verification) belong to
blfs_manager.downloader and are covered in tests/test_downloader.py. Here the
Downloader is stubbed so these tests pin the mapping from database entry to
(filename, mirrors, hash) and the handling of packages that are not downloadable.
"""

import pytest

from blfs_manager import commands as commands_module
from blfs_manager.commands import Commands
from blfs_manager.downloader import ChecksumError, DownloadResult, DownloadStatus
from tests.conftest import entry

TARBALL = 'https://example.invalid/pkg-1.0.tar.xz'
MIRROR = 'https://mirror.invalid/pkg-1.0.tar.xz'
PATCH = 'https://www.linuxfromscratch.org/patches/blfs/11.3/pkg-1.0-fix-1.patch'
MD5 = '0123456789abcdef0123456789abcdef'


class FakeDownloader:
    """Records fetch() calls instead of touching the network."""

    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def fetch(self, urls, destination, expected_hash=None):
        self.calls.append((list(urls), destination, expected_hash))
        if self.error is not None:
            raise self.error
        return DownloadResult(filename=destination,
                              status=DownloadStatus.DOWNLOADED,
                              url=list(urls)[0])

    @property
    def filenames(self):
        return [destination for _, destination, _ in self.calls]


@pytest.fixture
def fake_downloader(monkeypatch, tmp_path):
    fetcher = FakeDownloader()
    monkeypatch.setattr(commands_module, 'Downloader', lambda *a, **kw: fetcher)
    monkeypatch.setattr(commands_module, 'check_dir', lambda: None)
    monkeypatch.chdir(tmp_path)
    return fetcher


def test_patch_url_is_downloaded_despite_single_hash(fake_downloader):
    db = {'pkg': entry('pkg', urls=[TARBALL, PATCH], hashes=[MD5])}
    Commands(db, []).download_deps(['pkg'])
    assert fake_downloader.filenames == ['pkg-1.0.tar.xz', 'pkg-1.0-fix-1.patch']


def test_hash_is_applied_to_the_tarball_only(fake_downloader):
    db = {'pkg': entry('pkg', urls=[TARBALL, PATCH], hashes=[MD5])}
    Commands(db, []).download_deps(['pkg'])
    by_name = {name: h for _, name, h in fake_downloader.calls}
    assert by_name['pkg-1.0.tar.xz'] == MD5
    assert by_name['pkg-1.0-fix-1.patch'] is None


def test_all_urls_downloaded_when_hashes_absent(fake_downloader):
    db = {'pkg': entry('pkg', urls=[TARBALL, PATCH], hashes=[])}
    Commands(db, []).download_deps(['pkg'])
    assert fake_downloader.filenames == ['pkg-1.0.tar.xz', 'pkg-1.0-fix-1.patch']


def test_urls_sharing_a_basename_are_grouped_as_mirrors(fake_downloader):
    db = {'pkg': entry('pkg', urls=[TARBALL, MIRROR], hashes=[MD5])}
    Commands(db, []).download_deps(['pkg'])
    assert len(fake_downloader.calls) == 1, 'mirrors are one logical file'
    mirrors, destination, hash_val = fake_downloader.calls[0]
    assert mirrors == [TARBALL, MIRROR]
    assert destination == 'pkg-1.0.tar.xz'
    assert hash_val == MD5


def test_checksum_error_propagates(fake_downloader, monkeypatch):
    fetcher = FakeDownloader(error=ChecksumError('does not match'))
    monkeypatch.setattr(commands_module, 'Downloader', lambda *a, **kw: fetcher)
    db = {'pkg': entry('pkg', urls=[TARBALL], hashes=[MD5])}
    with pytest.raises(OSError, match='does not match'):
        Commands(db, []).download_deps(['pkg'])


def test_transport_failure_is_reported_not_raised(monkeypatch, tmp_path, caplog):
    """fetch() reports transport failures via the result, not an exception."""
    class FailingDownloader(FakeDownloader):
        def fetch(self, urls, destination, expected_hash=None):
            self.calls.append((list(urls), destination, expected_hash))
            return DownloadResult(filename=destination,
                                  status=DownloadStatus.FAILED,
                                  error='connection reset')

    fetcher = FailingDownloader()
    monkeypatch.setattr(commands_module, 'Downloader', lambda *a, **kw: fetcher)
    monkeypatch.setattr(commands_module, 'check_dir', lambda: None)
    monkeypatch.chdir(tmp_path)

    db = {'pkg': entry('pkg', urls=[TARBALL, PATCH], hashes=[MD5])}
    Commands(db, []).download_deps(['pkg'])

    assert len(fetcher.calls) == 2, 'a failed file must not abort the queue'
    assert 'connection reset' in caplog.text


def test_non_archive_urls_are_skipped(fake_downloader):
    db = {'pkg': entry('pkg', urls=['https://example.invalid/homepage.html'],
                       hashes=[])}
    Commands(db, []).download_deps(['pkg'])
    assert fake_downloader.calls == []


def test_book_section_is_skipped_not_downloaded(fake_downloader):
    db = {'Xorg Libraries': entry('Xorg Libraries', urls=[TARBALL], hashes=[])}
    Commands(db, []).download_deps(['Xorg Libraries'])
    assert fake_downloader.calls == []


def test_unknown_package_does_not_crash(fake_downloader):
    Commands({}, []).download_deps(['Cantarell fonts'])
    assert fake_downloader.calls == []


def test_every_package_in_the_queue_is_fetched(fake_downloader):
    db = {
        'a': entry('a', urls=['https://x.invalid/a-1.tar.gz'], hashes=[MD5]),
        'b': entry('b', urls=['https://x.invalid/b-1.tar.gz'], hashes=[MD5]),
    }
    Commands(db, []).download_deps(['a', 'b'])
    assert fake_downloader.filenames == ['a-1.tar.gz', 'b-1.tar.gz']
