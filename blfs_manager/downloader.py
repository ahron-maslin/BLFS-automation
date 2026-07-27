"""Source-archive downloader built on `requests`.

Replaces the `wget` PyPI package (3.2, unmaintained since 2015), which offers
no TLS-verification control, no timeout, no resume and no mirror handling.

Every transfer streams to a sibling `<name>.part` file and is renamed into
place only after the bytes are on disk, so an interrupted download can never be
mistaken for a complete archive. A leftover `.part` is resumed with an HTTP
`Range` request where the server supports it.
"""

import hashlib
import logging
import os
import re
from dataclasses import dataclass

import requests
from termcolor import colored

from .define import HEADERS

# (connect, read) - a stalled mirror must not hang a 100-package build queue.
DEFAULT_TIMEOUT = (10.0, 60.0)
DEFAULT_CHUNK_SIZE = 256 * 1024
PART_SUFFIX = '.part'
MD5_RE = re.compile(r'[0-9a-fA-F]{32}')


class DownloadError(OSError):
    """Raised when a file could not be retrieved from any candidate URL."""


class ChecksumError(OSError):
    """Raised when a file's MD5 sum does not match the one the book records."""


class DownloadStatus:
    """Outcome of a single logical file request."""

    DOWNLOADED = 'downloaded'
    CACHED = 'cached'
    FAILED = 'failed'


@dataclass
class DownloadResult:
    """Reports what happened to one logical file.

    Attributes:
        filename (str): The destination path that was requested.
        status (str): One of the DownloadStatus constants.
        url (str | None): The URL that succeeded, if any.
        error (str | None): Why every candidate URL failed, if it did.
        bytes_written (int): Bytes fetched over the network this call.
        resumed (bool): True if the transfer continued a partial file.
        verified (bool): True if the MD5 sum was checked and matched.
    """

    filename: str
    status: str
    url: str | None = None
    error: str | None = None
    bytes_written: int = 0
    resumed: bool = False
    verified: bool = False

    @property
    def ok(self):
        """bool: True if the file is present and usable after the call."""
        return self.status in (DownloadStatus.DOWNLOADED, DownloadStatus.CACHED)


def is_usable_hash(value):
    """Reports whether a recorded hash can actually be checked.

    BLFS patches carry no MD5 sum at all, and the book's `install-tl-unx` entry
    scrapes as the literal word "frequently", so a recorded hash is only
    trustworthy when it looks like one.

    Args:
        value (str | None): The hash as recorded in the database.

    Returns:
        bool: True if the value is 32 hexadecimal characters.

    """
    if not value:
        return False
    return bool(MD5_RE.fullmatch(str(value).strip()))


def md5_of_file(path, chunk_size=1024 * 1024):
    """Computes the MD5 digest of a file without reading it into memory.

    Args:
        path (str): The file to hash.
        chunk_size (int): Bytes to read per iteration.

    Returns:
        str: The lowercase hexadecimal digest.

    """
    digest = hashlib.md5()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b''):
            digest.update(chunk)
    return digest.hexdigest()


def verify_file(path, expected_hash, remove_on_mismatch=True):
    """Checks a file against its expected MD5 sum.

    Args:
        path (str): The file to verify.
        expected_hash (str | None): The MD5 sum recorded for it.
        remove_on_mismatch (bool): Delete the file when it does not match.

    Returns:
        bool: True if the sum was checked and matched, False if no usable sum
            was recorded and verification was skipped.

    Raises:
        ChecksumError: If a usable sum was recorded and the file does not
            match it.

    """
    if not is_usable_hash(expected_hash):
        logging.warning(colored(
            f'No usable MD5 sum recorded for {os.path.basename(path)} - '
            f'integrity NOT verified.', 'yellow'))
        return False

    actual = md5_of_file(path)
    if actual != str(expected_hash).strip().lower():
        if remove_on_mismatch and os.path.isfile(path):
            os.remove(path)
        raise ChecksumError(
            f'Downloaded file does not match the MD5 hash!\n'
            f'  file:     {path}\n'
            f'  expected: {expected_hash}\n'
            f'  actual:   {actual}\n')
    return True


class Downloader:
    """Fetches source archives over HTTP(S) with resume and mirror fallback.

    A single instance holds one `requests.Session` so a build queue reuses
    connections instead of renegotiating TLS for every package.

    Args:
        session (requests.Session | None): Session to use; one is created if
            omitted.
        timeout (tuple | float): Passed straight to requests as the
            (connect, read) timeout.
        chunk_size (int): Bytes written per iteration while streaming.
        resume (bool): Continue from a leftover `.part` file when possible.
        verify_tls (bool): Verify server certificates. Never disable this for
            real downloads - archives are later built and installed as root.
        headers (dict | None): Extra request headers.
        progress (callable | None): Called as `progress(done, total)` after
            each chunk, where `total` may be None if the server sent no length.

    Attributes:
        session (requests.Session): The session used for every request.

    """

    def __init__(self, session=None, timeout=DEFAULT_TIMEOUT,
                 chunk_size=DEFAULT_CHUNK_SIZE, resume=True, verify_tls=True,
                 headers=None, progress=None):
        self.session = session if session is not None else requests.Session()
        self._owns_session = session is None
        self.timeout = timeout
        self.chunk_size = chunk_size
        self.resume = resume
        self.verify_tls = verify_tls
        self.headers = dict(headers) if headers else dict(HEADERS)
        self.progress = progress

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
        return False

    def close(self):
        """Closes the session if this instance created it.

        Returns:
            None

        """
        if self._owns_session:
            self.session.close()

    def fetch(self, urls, destination, expected_hash=None):
        """Retrieves one logical file, trying each candidate URL in turn.

        An already-present destination file is verified before it is reused; a
        cached file whose sum is wrong is discarded and downloaded again rather
        than trusted, because the BLFS book records only one sum per package
        and a truncated cache entry would otherwise be built as root.

        Args:
            urls (str | list): One URL, or mirrors for the same logical file.
            destination (str): Path the finished file should end up at.
            expected_hash (str | None): The MD5 sum recorded for the file.

        Returns:
            DownloadResult: What happened, and why if it failed.

        Raises:
            ChecksumError: If a freshly downloaded file does not match a
                usable recorded MD5 sum.

        """
        candidates = [urls] if isinstance(urls, str) else [u for u in (urls or [])]
        candidates = [str(u).strip() for u in candidates if u]
        name = os.path.basename(destination)

        cached = self._reuse_cached(destination, expected_hash)
        if cached is not None:
            return cached

        if not candidates:
            message = f'No download URL available for {name}'
            logging.error(colored(message, 'red'))
            return DownloadResult(destination, DownloadStatus.FAILED, error=message)

        self._ensure_parent(destination)
        part_path = f'{destination}{PART_SUFFIX}'
        failures = []

        for url in candidates:
            logging.info(colored(f'Downloading: {url}', 'green'))
            try:
                written, resumed = self._fetch_one(url, destination, part_path)
            except (requests.RequestException, OSError) as exc:
                failures.append(f'{url}: {exc}')
                logging.error(colored(f'Failed to download {url}: {exc}', 'red'))
                continue

            verified = verify_file(destination, expected_hash)
            logging.info(colored(f'Successfully downloaded {name}', 'green'))
            return DownloadResult(destination, DownloadStatus.DOWNLOADED,
                                  url=url, bytes_written=written,
                                  resumed=resumed, verified=verified)

        error = '; '.join(failures)
        logging.error(colored(
            f'All {len(candidates)} source URLs failed for {name}.', 'red'))
        return DownloadResult(destination, DownloadStatus.FAILED, error=error)

    def _reuse_cached(self, destination, expected_hash):
        """Decides whether an already-present file can be reused.

        Args:
            destination (str): Path the finished file should end up at.
            expected_hash (str | None): The MD5 sum recorded for the file.

        Returns:
            DownloadResult | None: A cached result, or None if the file is
                absent or was discarded as corrupt.

        """
        if not os.path.isfile(destination):
            return None

        name = os.path.basename(destination)
        try:
            verified = verify_file(destination, expected_hash,
                                   remove_on_mismatch=False)
        except ChecksumError as exc:
            logging.warning(colored(
                f'Cached {name} fails its MD5 check - discarding it and '
                f'downloading again.\n{exc}', 'yellow'))
            os.remove(destination)
            return None

        logging.info(colored(f'{name} already has been downloaded', 'blue'))
        return DownloadResult(destination, DownloadStatus.CACHED,
                              verified=verified)

    def _fetch_one(self, url, destination, part_path):
        """Streams a single URL into place, resuming where possible.

        Args:
            url (str): The URL to request.
            destination (str): Path the finished file should end up at.
            part_path (str): Path of the partial file backing the transfer.

        Returns:
            tuple: (bytes written this call, whether the transfer resumed).

        Raises:
            requests.RequestException: On any transport or HTTP error.
            OSError: If the partial file cannot be written or renamed.

        """
        offset = self._resume_offset(part_path)
        response = self._request(url, offset)

        # 416 means the partial file is already at or past the resource size -
        # it belongs to a different file, so start over instead of giving up.
        if offset and response.status_code == 416:
            response.close()
            logging.warning(colored(
                f'Server rejected the resume range for {url} - '
                f'restarting the download.', 'yellow'))
            self._discard_partial(part_path)
            offset = 0
            response = self._request(url, 0)

        response.raise_for_status()

        if offset and response.status_code != 206:
            logging.info(colored(
                f'{url} does not support resuming - '
                f'restarting from the beginning.', 'yellow'))
            offset = 0

        resumed = offset > 0
        total = self._total_size(response, offset)
        written = 0

        with response:
            with open(part_path, 'ab' if resumed else 'wb') as handle:
                for chunk in response.iter_content(chunk_size=self.chunk_size):
                    if not chunk:
                        continue
                    handle.write(chunk)
                    written += len(chunk)
                    if self.progress:
                        self.progress(offset + written, total)

        os.replace(part_path, destination)
        return written, resumed

    def _request(self, url, offset):
        """Issues a streaming GET, asking to resume when offset is non-zero.

        Args:
            url (str): The URL to request.
            offset (int): Byte offset to resume from; 0 for a full request.

        Returns:
            requests.Response: An unconsumed streaming response.

        """
        headers = dict(self.headers)
        if offset:
            headers['Range'] = f'bytes={offset}-'
        return self.session.get(url, headers=headers, timeout=self.timeout,
                                stream=True, verify=self.verify_tls,
                                allow_redirects=True)

    def _resume_offset(self, part_path):
        """Returns the byte offset a leftover partial file can resume from.

        Args:
            part_path (str): Path of the partial file.

        Returns:
            int: The size of the partial file, or 0 if resuming is off or no
                usable partial file exists.

        """
        if not self.resume or not os.path.isfile(part_path):
            return 0
        return os.path.getsize(part_path)

    @staticmethod
    def _discard_partial(part_path):
        """Removes a partial file, ignoring its absence.

        Args:
            part_path (str): Path of the partial file.

        Returns:
            None

        """
        try:
            os.remove(part_path)
        except OSError:
            pass

    @staticmethod
    def _total_size(response, offset):
        """Works out the full size of the resource being fetched.

        Args:
            response (requests.Response): The streaming response.
            offset (int): Bytes already on disk from a resumed transfer.

        Returns:
            int | None: The total size in bytes, or None if unknown.

        """
        length = response.headers.get('Content-Length')
        if length is None:
            return None
        try:
            return offset + int(length)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _ensure_parent(destination):
        """Creates the destination's parent directory if it is missing.

        Args:
            destination (str): Path the finished file should end up at.

        Returns:
            None

        """
        parent = os.path.dirname(destination)
        if parent:
            os.makedirs(parent, exist_ok=True)


def download_file(urls, destination, expected_hash=None, session=None,
                  timeout=DEFAULT_TIMEOUT, chunk_size=DEFAULT_CHUNK_SIZE,
                  resume=True, verify_tls=True, headers=None, progress=None):
    """Downloads one logical file, creating a throwaway Downloader.

    Convenience wrapper for callers with a single file to fetch. Prefer a
    long-lived `Downloader` when working through a build queue so connections
    are reused.

    Args:
        urls (str | list): One URL, or mirrors for the same logical file.
        destination (str): Path the finished file should end up at.
        expected_hash (str | None): The MD5 sum recorded for the file.
        session (requests.Session | None): Session to reuse, if any.
        timeout (tuple | float): The (connect, read) timeout.
        chunk_size (int): Bytes written per iteration while streaming.
        resume (bool): Continue from a leftover `.part` file when possible.
        verify_tls (bool): Verify server certificates.
        headers (dict | None): Extra request headers.
        progress (callable | None): Called as `progress(done, total)`.

    Returns:
        DownloadResult: What happened, and why if it failed.

    Raises:
        ChecksumError: If a freshly downloaded file does not match a usable
            recorded MD5 sum.

    """
    downloader = Downloader(session=session, timeout=timeout,
                            chunk_size=chunk_size, resume=resume,
                            verify_tls=verify_tls, headers=headers,
                            progress=progress)
    try:
        return downloader.fetch(urls, destination, expected_hash)
    finally:
        downloader.close()
