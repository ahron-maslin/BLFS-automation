"""Tests for extraction safety, checksum handling and command execution."""

import hashlib
import io
import os
import tarfile

import pytest

from blfs_manager.utils import (
    change_dir, is_within_directory, md5_check, run_cmd, safe_extract,
)


class TestIsWithinDirectory:
    def test_child_is_inside(self, tmp_path):
        assert is_within_directory(str(tmp_path), str(tmp_path / 'a' / 'b'))

    def test_parent_is_outside(self, tmp_path):
        assert not is_within_directory(str(tmp_path / 'foo'), str(tmp_path))

    def test_traversal_is_outside(self, tmp_path):
        assert not is_within_directory(str(tmp_path), str(tmp_path / '..' / 'evil'))

    def test_sibling_sharing_name_prefix_is_outside(self, tmp_path):
        # os.path.commonprefix() is a string op and called this "inside".
        (tmp_path / 'foo').mkdir()
        (tmp_path / 'foo-evil').mkdir()
        assert not is_within_directory(
            str(tmp_path / 'foo'), str(tmp_path / 'foo-evil' / 'payload'))


class TestSafeExtract:
    def _tar_with(self, path, name, linkname=None, link_type=None):
        with tarfile.open(path, 'w') as tar:
            if link_type is None:
                data = b'payload'
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            else:
                info = tarfile.TarInfo(name)
                info.type = link_type
                info.linkname = linkname
                tar.addfile(info)
        return path

    def test_extracts_normal_archive(self, tmp_path):
        archive = self._tar_with(tmp_path / 'ok.tar', 'pkg-1.0/file.txt')
        dest = tmp_path / 'out'
        dest.mkdir()
        with tarfile.open(archive) as tar:
            safe_extract(tar, str(dest))
        assert (dest / 'pkg-1.0' / 'file.txt').read_bytes() == b'payload'

    def test_rejects_path_traversal(self, tmp_path):
        archive = self._tar_with(tmp_path / 'evil.tar', '../escaped.txt')
        dest = tmp_path / 'out'
        dest.mkdir()
        with tarfile.open(archive) as tar:
            with pytest.raises(Exception, match='Path Traversal'):
                safe_extract(tar, str(dest))
        assert not (tmp_path / 'escaped.txt').exists()

    def test_rejects_absolute_path(self, tmp_path):
        archive = self._tar_with(tmp_path / 'abs.tar', '/etc/passwd')
        dest = tmp_path / 'out'
        dest.mkdir()
        with tarfile.open(archive) as tar:
            with pytest.raises(Exception, match='Traversal'):
                safe_extract(tar, str(dest))

    def test_rejects_symlink_escaping_root(self, tmp_path):
        archive = self._tar_with(
            tmp_path / 'link.tar', 'pkg/link',
            linkname='../../../../etc/passwd', link_type=tarfile.SYMTYPE)
        dest = tmp_path / 'out'
        dest.mkdir()
        with tarfile.open(archive) as tar:
            with pytest.raises(Exception, match='Traversal'):
                safe_extract(tar, str(dest))


class TestMd5Check:
    def _file(self, tmp_path, content=b'blfs'):
        target = tmp_path / 'src.tar.gz'
        target.write_bytes(content)
        return target, hashlib.md5(content).hexdigest()

    def test_accepts_matching_hash(self, tmp_path):
        target, digest = self._file(tmp_path)
        md5_check(str(target), digest)
        assert target.exists()

    def test_is_case_insensitive(self, tmp_path):
        target, digest = self._file(tmp_path)
        md5_check(str(target), digest.upper())
        assert target.exists()

    def test_rejects_and_removes_corrupt_download(self, tmp_path):
        target, _ = self._file(tmp_path)
        with pytest.raises(OSError, match='does not match'):
            md5_check(str(target), '0' * 32)
        assert not target.exists(), 'corrupt file must not be left behind'

    def test_missing_hash_warns_but_keeps_file(self, tmp_path):
        # Patch URLs carry no MD5 sum in the book.
        target, _ = self._file(tmp_path)
        md5_check(str(target), None)
        assert target.exists()

    def test_non_md5_hash_is_ignored(self, tmp_path):
        # The book scrape yields junk like "frequently" for install-tl-unx.
        target, _ = self._file(tmp_path)
        md5_check(str(target), 'frequently')
        assert target.exists()

    def test_large_file_is_streamed(self, tmp_path):
        content = b'x' * (4 * 1024 * 1024 + 7)
        target, digest = self._file(tmp_path, content)
        md5_check(str(target), digest)


class TestChangeDir:
    def test_extracts_cd_target(self):
        assert change_dir(['cd', 'build']) == 'build'

    def test_returns_empty_without_cd(self):
        assert change_dir(['make', 'install']) == ''

    def test_finds_cd_mid_command(self):
        assert change_dir(['mkdir', 'build', '&&', 'cd', 'build']) == 'build'


class TestRunCmd:
    def test_returns_zero_on_success(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert run_cmd('true') == 0

    def test_reports_failure(self, tmp_path, monkeypatch):
        # Previously the exit status was discarded and builds "succeeded".
        monkeypatch.chdir(tmp_path)
        assert run_cmd('exit 3') == 3

    def test_failing_command_does_not_change_directory(self, tmp_path, monkeypatch):
        (tmp_path / 'build').mkdir()
        monkeypatch.chdir(tmp_path)
        run_cmd('false && cd build')
        assert os.path.realpath(os.getcwd()) == os.path.realpath(tmp_path)

    def test_cd_persists_into_the_process(self, tmp_path, monkeypatch):
        (tmp_path / 'build').mkdir()
        monkeypatch.chdir(tmp_path)
        assert run_cmd('cd build') == 0
        assert os.path.realpath(os.getcwd()) == os.path.realpath(tmp_path / 'build')
