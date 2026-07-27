"""Tests for the requests-based downloader.

No test here touches the network: every `requests.Session` is a stub that
replays canned responses and records the requests it was given.

Behaviour these tests pin down, where the module had a choice:

* A cached file whose recorded MD5 sum does not match is **discarded and
  downloaded again** rather than raising. The book records one sum per
  package, so a mismatching cache entry is far more likely to be a truncated
  earlier download than a bad book entry.
* A server that ignores `Range` and answers 200 causes a clean restart from
  byte zero; a server answering 416 (partial file at or past the resource
  size) causes the partial file to be dropped and the request retried once.
* A failed transfer keeps its `.part` file so the next attempt can resume,
  but never renames it into place.
"""

import hashlib
import os

import pytest
import requests

from blfs_manager.downloader import (
    ChecksumError,
    Downloader,
    DownloadStatus,
    download_file,
    is_usable_hash,
)

TARBALL = 'https://example.invalid/pkg-1.0.tar.xz'
MIRROR = 'https://mirror.invalid/pkg-1.0.tar.xz'
BODY = b'a-source-tarball-payload'
BODY_MD5 = hashlib.md5(BODY).hexdigest()


class FakeResponse:
    """Stands in for a streaming `requests.Response`."""

    def __init__(self, body=b'', status_code=200, headers=None):
        self.body = body
        self.status_code = status_code
        self.headers = headers if headers is not None else {
            'Content-Length': str(len(body))}
        self.closed = False
        self.chunk_sizes = []

    def iter_content(self, chunk_size=1):
        self.chunk_sizes.append(chunk_size)
        for start in range(0, len(self.body), chunk_size):
            yield self.body[start:start + chunk_size]
        # Real servers emit keep-alive chunks; they must not be written.
        yield b''

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f'{self.status_code} error', response=self)

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
        return False


class FakeSession:
    """Replays queued responses and records every request it receives."""

    def __init__(self, handler):
        self.handler = handler
        self.calls = []
        self.closed = False

    def get(self, url, **kwargs):
        self.calls.append({'url': url, **kwargs})
        outcome = self.handler(url, kwargs)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def close(self):
        self.closed = True

    @property
    def urls(self):
        return [call['url'] for call in self.calls]

    @property
    def ranges(self):
        return [call['headers'].get('Range') for call in self.calls]


def session_for(*outcomes):
    """Builds a FakeSession that returns each outcome in order."""
    queue = list(outcomes)

    def handler(url, kwargs):
        return queue.pop(0) if queue else FakeResponse(BODY)

    return FakeSession(handler)


def session_by_url(mapping):
    """Builds a FakeSession keyed by URL."""

    def handler(url, kwargs):
        return mapping[url]

    return FakeSession(handler)


@pytest.fixture
def dest(tmp_path):
    return str(tmp_path / 'pkg-1.0.tar.xz')


# --- happy path ------------------------------------------------------------

def test_successful_download_writes_file_and_verifies(dest):
    session = session_for(FakeResponse(BODY))
    result = Downloader(session=session).fetch([TARBALL], dest, BODY_MD5)

    assert result.status == DownloadStatus.DOWNLOADED
    assert result.ok and result.verified
    assert result.url == TARBALL
    assert result.bytes_written == len(BODY)
    assert open(dest, 'rb').read() == BODY


def test_request_is_streamed_with_tls_verification_and_timeout(dest):
    session = session_for(FakeResponse(BODY))
    Downloader(session=session, timeout=(3, 7), chunk_size=8).fetch(
        [TARBALL], dest, BODY_MD5)

    call = session.calls[0]
    assert call['stream'] is True
    assert call['verify'] is True
    assert call['timeout'] == (3, 7)
    assert call['headers'].get('Range') is None


def test_body_is_streamed_in_chunks_not_read_whole(dest):
    response = FakeResponse(BODY)
    Downloader(session=session_for(response), chunk_size=4).fetch([TARBALL], dest)

    assert response.chunk_sizes == [4]
    assert response.closed


def test_no_part_file_remains_after_success(dest):
    Downloader(session=session_for(FakeResponse(BODY))).fetch([TARBALL], dest)

    assert not os.path.exists(f'{dest}.part')


def test_progress_callback_receives_running_and_total_size(dest):
    seen = []
    Downloader(session=session_for(FakeResponse(BODY)), chunk_size=8,
               progress=lambda done, total: seen.append((done, total))).fetch(
        [TARBALL], dest)

    assert seen[-1] == (len(BODY), len(BODY))
    assert [done for done, _ in seen] == sorted(done for done, _ in seen)


# --- checksum handling -----------------------------------------------------

def test_checksum_mismatch_deletes_file_and_raises(dest):
    session = session_for(FakeResponse(BODY))
    with pytest.raises(ChecksumError, match='does not match'):
        Downloader(session=session).fetch([TARBALL], dest, '0' * 32)

    assert not os.path.exists(dest)
    assert not os.path.exists(f'{dest}.part')


def test_absent_hash_skips_verification(dest):
    result = Downloader(session=session_for(FakeResponse(BODY))).fetch(
        [TARBALL], dest, None)

    assert result.ok and not result.verified
    assert open(dest, 'rb').read() == BODY


@pytest.mark.parametrize('junk', ['frequently', '', 'not-a-hash', 'abc123',
                                  '0' * 31, '0' * 33, 'z' * 32, None])
def test_junk_hash_skips_verification_instead_of_failing(dest, junk):
    assert is_usable_hash(junk) is False
    result = Downloader(session=session_for(FakeResponse(BODY))).fetch(
        [TARBALL], dest, junk)

    assert result.status == DownloadStatus.DOWNLOADED
    assert result.verified is False


def test_uppercase_and_padded_hash_is_accepted(dest):
    result = Downloader(session=session_for(FakeResponse(BODY))).fetch(
        [TARBALL], dest, f'  {BODY_MD5.upper()}  ')

    assert result.verified is True


# --- cache reuse -----------------------------------------------------------

def test_cached_file_with_correct_hash_is_reused_without_a_request(dest):
    with open(dest, 'wb') as handle:
        handle.write(BODY)
    session = session_for(FakeResponse(b'should not be requested'))

    result = Downloader(session=session).fetch([TARBALL], dest, BODY_MD5)

    assert result.status == DownloadStatus.CACHED
    assert result.verified is True
    assert session.calls == []


def test_cached_file_without_a_usable_hash_is_reused_unverified(dest):
    with open(dest, 'wb') as handle:
        handle.write(b'whatever')
    session = session_for(FakeResponse(BODY))

    result = Downloader(session=session).fetch([TARBALL], dest, 'frequently')

    assert result.status == DownloadStatus.CACHED
    assert result.verified is False
    assert session.calls == []


def test_cached_file_with_wrong_hash_is_discarded_and_refetched(dest):
    with open(dest, 'wb') as handle:
        handle.write(b'truncated cache entry')
    session = session_for(FakeResponse(BODY))

    result = Downloader(session=session).fetch([TARBALL], dest, BODY_MD5)

    assert result.status == DownloadStatus.DOWNLOADED
    assert result.verified is True
    assert session.urls == [TARBALL]
    assert open(dest, 'rb').read() == BODY


def test_corrupt_cache_is_removed_even_when_the_refetch_fails(dest):
    with open(dest, 'wb') as handle:
        handle.write(b'truncated cache entry')
    session = session_for(requests.ConnectionError('no route to host'))

    result = Downloader(session=session).fetch([TARBALL], dest, BODY_MD5)

    assert result.status == DownloadStatus.FAILED
    assert not os.path.exists(dest), 'a corrupt cache entry must not survive'


# --- resume ----------------------------------------------------------------

def test_resume_sends_range_header_and_appends_to_part_file(dest):
    with open(f'{dest}.part', 'wb') as handle:
        handle.write(BODY[:10])
    response = FakeResponse(BODY[10:], status_code=206,
                            headers={'Content-Length': str(len(BODY) - 10)})

    result = Downloader(session=session_for(response)).fetch(
        [TARBALL], dest, BODY_MD5)

    assert result.resumed is True
    assert result.bytes_written == len(BODY) - 10
    assert open(dest, 'rb').read() == BODY


def test_resume_request_asks_for_the_right_offset(dest):
    with open(f'{dest}.part', 'wb') as handle:
        handle.write(BODY[:10])
    session = session_for(FakeResponse(BODY[10:], status_code=206))

    Downloader(session=session).fetch([TARBALL], dest, BODY_MD5)

    assert session.ranges == ['bytes=10-']


def test_server_ignoring_range_restarts_cleanly(dest):
    with open(f'{dest}.part', 'wb') as handle:
        handle.write(b'stale leading bytes')
    session = session_for(FakeResponse(BODY, status_code=200))

    result = Downloader(session=session).fetch([TARBALL], dest, BODY_MD5)

    assert session.ranges == ['bytes=19-'], 'resume must still be attempted'
    assert result.resumed is False
    assert open(dest, 'rb').read() == BODY, 'stale bytes must not be kept'


def test_range_not_satisfiable_drops_the_partial_and_retries(dest):
    with open(f'{dest}.part', 'wb') as handle:
        handle.write(b'x' * 999)
    session = session_for(FakeResponse(b'', status_code=416),
                          FakeResponse(BODY, status_code=200))

    result = Downloader(session=session).fetch([TARBALL], dest, BODY_MD5)

    assert session.ranges == ['bytes=999-', None]
    assert result.resumed is False
    assert open(dest, 'rb').read() == BODY


def test_resume_disabled_ignores_an_existing_part_file(dest):
    with open(f'{dest}.part', 'wb') as handle:
        handle.write(BODY[:10])
    session = session_for(FakeResponse(BODY))

    result = Downloader(session=session, resume=False).fetch(
        [TARBALL], dest, BODY_MD5)

    assert session.ranges == [None]
    assert result.resumed is False
    assert open(dest, 'rb').read() == BODY


def test_failed_transfer_keeps_the_part_file_for_a_later_resume(dest):
    class HalfBody(FakeResponse):
        def iter_content(self, chunk_size=1):
            yield BODY[:10]
            raise requests.ConnectionError('connection reset')

    result = Downloader(session=session_for(HalfBody(BODY))).fetch(
        [TARBALL], dest, BODY_MD5)

    assert result.status == DownloadStatus.FAILED
    assert not os.path.exists(dest), (
        'an interrupted transfer must never appear as a complete file')
    assert open(f'{dest}.part', 'rb').read() == BODY[:10]


def test_interrupted_transfer_is_completed_by_a_second_call(dest):
    class HalfBody(FakeResponse):
        def iter_content(self, chunk_size=1):
            yield BODY[:10]
            raise requests.ConnectionError('connection reset')

    downloader = Downloader(session=session_for(
        HalfBody(BODY),
        FakeResponse(BODY[10:], status_code=206)))

    first = downloader.fetch([TARBALL], dest, BODY_MD5)
    second = downloader.fetch([TARBALL], dest, BODY_MD5)

    assert first.status == DownloadStatus.FAILED
    assert second.status == DownloadStatus.DOWNLOADED
    assert second.resumed is True
    assert open(dest, 'rb').read() == BODY


# --- mirrors and failures --------------------------------------------------

def test_second_mirror_is_tried_after_the_first_fails(dest):
    session = session_by_url({
        TARBALL: requests.ConnectionError('name resolution failed'),
        MIRROR: FakeResponse(BODY),
    })

    result = Downloader(session=session).fetch([TARBALL, MIRROR], dest, BODY_MD5)

    assert result.status == DownloadStatus.DOWNLOADED
    assert result.url == MIRROR
    assert session.urls == [TARBALL, MIRROR]


def test_http_error_on_the_first_mirror_falls_through(dest):
    session = session_by_url({
        TARBALL: FakeResponse(b'not found', status_code=404),
        MIRROR: FakeResponse(BODY),
    })

    result = Downloader(session=session).fetch([TARBALL, MIRROR], dest, BODY_MD5)

    assert result.ok and result.url == MIRROR


def test_all_mirrors_failing_is_reported_cleanly(dest):
    session = session_by_url({
        TARBALL: requests.ConnectionError('name resolution failed'),
        MIRROR: requests.HTTPError('503 error'),
    })

    result = Downloader(session=session).fetch([TARBALL, MIRROR], dest, BODY_MD5)

    assert result.status == DownloadStatus.FAILED
    assert result.ok is False
    assert TARBALL in result.error and MIRROR in result.error
    assert not os.path.exists(dest)


def test_timeout_is_handled_without_crashing(dest):
    session = session_for(requests.Timeout('read timed out'))

    result = Downloader(session=session).fetch([TARBALL], dest, BODY_MD5)

    assert result.status == DownloadStatus.FAILED
    assert 'timed out' in result.error


def test_disk_error_is_reported_as_a_failure_not_an_exception(dest, monkeypatch):
    def refuse(*args, **kwargs):
        raise PermissionError('read-only file system')

    monkeypatch.setattr('blfs_manager.downloader.open', refuse, raising=False)
    result = Downloader(session=session_for(FakeResponse(BODY))).fetch(
        [TARBALL], dest, BODY_MD5)

    assert result.status == DownloadStatus.FAILED
    assert 'read-only' in result.error


def test_empty_url_list_is_reported_not_raised(dest):
    result = Downloader(session=session_for()).fetch([], dest, BODY_MD5)

    assert result.status == DownloadStatus.FAILED
    assert 'No download URL' in result.error


def test_none_urls_are_ignored(dest):
    session = session_for(FakeResponse(BODY))
    result = Downloader(session=session).fetch([None, '', TARBALL], dest)

    assert session.urls == [TARBALL]
    assert result.ok


# --- API shape -------------------------------------------------------------

def test_a_single_url_string_is_accepted(dest):
    result = Downloader(session=session_for(FakeResponse(BODY))).fetch(
        TARBALL, dest, BODY_MD5)

    assert result.ok and result.url == TARBALL


def test_session_is_reused_across_fetches(tmp_path):
    session = session_for(FakeResponse(BODY), FakeResponse(BODY))
    downloader = Downloader(session=session)

    downloader.fetch([TARBALL], str(tmp_path / 'one.tar.xz'), BODY_MD5)
    downloader.fetch([MIRROR], str(tmp_path / 'two.tar.xz'), BODY_MD5)

    assert len(session.calls) == 2


def test_a_caller_supplied_session_is_not_closed(dest):
    session = session_for(FakeResponse(BODY))
    with Downloader(session=session) as downloader:
        downloader.fetch([TARBALL], dest, BODY_MD5)

    assert session.closed is False


def test_download_file_wrapper_closes_its_own_session(dest, monkeypatch):
    created = []

    def make_session():
        session = session_for(FakeResponse(BODY))
        created.append(session)
        return session

    monkeypatch.setattr(requests, 'Session', make_session)
    result = download_file([TARBALL], dest, BODY_MD5)

    assert result.status == DownloadStatus.DOWNLOADED
    assert created[0].closed is True


def test_missing_parent_directory_is_created(tmp_path):
    dest = str(tmp_path / 'sources' / 'pkg-1.0.tar.xz')
    result = Downloader(session=session_for(FakeResponse(BODY))).fetch(
        [TARBALL], dest, BODY_MD5)

    assert result.ok and os.path.isfile(dest)


def test_missing_content_length_is_tolerated(dest):
    response = FakeResponse(BODY, headers={})
    seen = []
    result = Downloader(session=session_for(response),
                        progress=lambda done, total: seen.append(total)).fetch(
        [TARBALL], dest, BODY_MD5)

    assert result.ok
    assert seen[-1] is None
