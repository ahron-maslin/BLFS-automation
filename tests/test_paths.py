"""Tests for state/cache location resolution, seeding and legacy migration."""

import os
import stat

import pytest

from blfs_manager import paths


ENV_VARS = (
    paths.STATE_DIR_ENV,
    paths.CACHE_DIR_ENV,
    'XDG_STATE_HOME',
    'XDG_CACHE_HOME',
)

running_as_root = pytest.mark.skipif(
    paths.is_root(), reason='root bypasses permission bits')


@pytest.fixture(autouse=True)
def clean_env(tmp_path, monkeypatch):
    """Isolates every test from the developer's real environment and HOME."""
    for var in ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setattr(paths.Path, 'home', classmethod(lambda cls: home))
    monkeypatch.setattr(os, 'geteuid', lambda: 1000)
    return home


@pytest.fixture
def as_root(monkeypatch):
    monkeypatch.setattr(os, 'geteuid', lambda: 0)


@pytest.fixture
def state_and_cache(tmp_path, monkeypatch):
    """Points both locations at tmp dirs that do not exist yet."""
    state = tmp_path / 'state'
    cache = tmp_path / 'cache'
    monkeypatch.setenv(paths.STATE_DIR_ENV, str(state))
    monkeypatch.setenv(paths.CACHE_DIR_ENV, str(cache))
    return state, cache


@pytest.fixture
def unwritable_dir(tmp_path):
    """A directory with no write bit, restored so tmp cleanup can succeed."""
    target = tmp_path / 'readonly'
    target.mkdir()
    target.chmod(0o500)
    yield target
    target.chmod(0o755)


class TestIsRoot:
    def test_true_for_uid_zero(self, as_root):
        assert paths.is_root()

    def test_false_for_regular_user(self):
        assert not paths.is_root()


class TestStateDir:
    def test_env_override_wins_over_root(self, tmp_path, monkeypatch, as_root):
        monkeypatch.setenv(paths.STATE_DIR_ENV, str(tmp_path / 'custom'))
        assert paths.state_dir() == tmp_path / 'custom'

    def test_root_uses_var_lib(self, as_root):
        assert paths.state_dir() == paths.ROOT_STATE_DIR

    def test_non_root_uses_xdg_state_home(self, tmp_path, monkeypatch):
        monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path / 'xdg'))
        assert paths.state_dir() == tmp_path / 'xdg' / 'blfs-pm'

    def test_non_root_default_is_local_state(self, clean_env):
        assert paths.state_dir() == clean_env / '.local/state/blfs-pm'

    def test_relative_xdg_value_is_ignored(self, clean_env, monkeypatch):
        # A relative XDG value is invalid and must not resolve against CWD.
        monkeypatch.setenv('XDG_STATE_HOME', 'relative/state')
        assert paths.state_dir() == clean_env / '.local/state/blfs-pm'

    def test_empty_env_value_is_treated_as_unset(self, clean_env, monkeypatch):
        monkeypatch.setenv(paths.STATE_DIR_ENV, '   ')
        assert paths.state_dir() == clean_env / '.local/state/blfs-pm'

    def test_tilde_in_override_is_expanded(self, clean_env, monkeypatch):
        monkeypatch.setenv('HOME', str(clean_env))
        monkeypatch.setenv(paths.STATE_DIR_ENV, '~/mystate')
        assert paths.state_dir() == clean_env / 'mystate'

    def test_resolution_creates_nothing(self, tmp_path, monkeypatch):
        target = tmp_path / 'never-created'
        monkeypatch.setenv(paths.STATE_DIR_ENV, str(target))
        paths.state_dir()
        paths.db_path()
        paths.installed_log_path()
        assert not target.exists()


class TestSourcesDir:
    def test_env_override_wins_over_root(self, tmp_path, monkeypatch, as_root):
        monkeypatch.setenv(paths.CACHE_DIR_ENV, str(tmp_path / 'srcs'))
        assert paths.sources_dir() == tmp_path / 'srcs'

    def test_override_is_used_verbatim(self, tmp_path, monkeypatch):
        monkeypatch.setenv(paths.CACHE_DIR_ENV, str(tmp_path / 'srcs'))
        assert paths.sources_dir().name == 'srcs'

    def test_root_uses_var_cache(self, as_root):
        assert paths.sources_dir() == paths.ROOT_CACHE_DIR

    def test_non_root_uses_xdg_cache_home(self, tmp_path, monkeypatch):
        monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path / 'xdgcache'))
        assert paths.sources_dir() == tmp_path / 'xdgcache' / 'blfs-pm' / 'sources'

    def test_non_root_default_is_dot_cache(self, clean_env):
        assert paths.sources_dir() == clean_env / '.cache/blfs-pm/sources'

    def test_state_and_sources_are_distinct(self, as_root):
        assert paths.state_dir() != paths.sources_dir()


class TestInstalledLogPath:
    def test_lives_in_state_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv(paths.STATE_DIR_ENV, str(tmp_path / 'state'))
        assert paths.installed_log_path() == tmp_path / 'state' / '.installed_log'


class TestJournalPath:
    def test_lives_in_state_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv(paths.STATE_DIR_ENV, str(tmp_path / 'state'))
        assert paths.journal_path() == tmp_path / 'state' / 'build.jsonl'

    def test_distinct_from_installed_log(self, tmp_path, monkeypatch):
        monkeypatch.setenv(paths.STATE_DIR_ENV, str(tmp_path / 'state'))
        assert paths.journal_path() != paths.installed_log_path()


class TestEnsureWritable:
    def test_creates_missing_directory(self, tmp_path):
        target = tmp_path / 'new'
        assert paths.ensure_writable(target) == target
        assert target.is_dir()

    def test_creates_parents(self, tmp_path):
        target = tmp_path / 'a' / 'b' / 'c'
        paths.ensure_writable(target)
        assert target.is_dir()

    def test_uses_0755(self, tmp_path):
        target = tmp_path / 'perm'
        paths.ensure_writable(target)
        assert stat.S_IMODE(target.stat().st_mode) == 0o755

    def test_is_idempotent(self, tmp_path):
        target = tmp_path / 'twice'
        paths.ensure_writable(target)
        (target / 'keep').write_text('x')
        paths.ensure_writable(target)
        assert (target / 'keep').read_text() == 'x'

    def test_leaves_no_probe_file_behind(self, tmp_path):
        target = tmp_path / 'probe'
        paths.ensure_writable(target)
        assert list(target.iterdir()) == []

    def test_rejects_a_file(self, tmp_path):
        target = tmp_path / 'afile'
        target.write_text('not a directory')
        with pytest.raises(NotADirectoryError):
            paths.ensure_writable(target)

    @running_as_root
    def test_raises_on_unwritable_directory(self, unwritable_dir):
        with pytest.raises(PermissionError):
            paths.ensure_writable(unwritable_dir)

    @running_as_root
    def test_raises_when_parent_is_unwritable(self, unwritable_dir):
        with pytest.raises(PermissionError):
            paths.ensure_writable(unwritable_dir / 'child')

    @running_as_root
    def test_error_names_the_env_overrides(self, unwritable_dir):
        with pytest.raises(PermissionError) as excinfo:
            paths.ensure_writable(unwritable_dir)
        message = str(excinfo.value)
        assert paths.STATE_DIR_ENV in message
        assert paths.CACHE_DIR_ENV in message
        assert str(unwritable_dir) in message


class TestPackagedDbPath:
    def test_finds_the_shipped_seed(self, tmp_path):
        seed = tmp_path / 'lfs-deps-11.3'
        seed.write_text('{}')
        assert paths.packaged_db_path(tmp_path) == seed

    def test_none_when_no_seed_ships(self, tmp_path):
        assert paths.packaged_db_path(tmp_path) is None

    def test_ignores_temporary_files(self, tmp_path):
        (tmp_path / 'lfs-deps-11.3.tmp').write_text('partial')
        assert paths.packaged_db_path(tmp_path) is None

    def test_picks_the_newest_edition(self, tmp_path):
        (tmp_path / 'lfs-deps-11.3').write_text('{}')
        newest = tmp_path / 'lfs-deps-12.0'
        newest.write_text('{}')
        assert paths.packaged_db_path(tmp_path) == newest


class TestDbPath:
    def test_uses_the_packaged_seed_name(self, tmp_path, monkeypatch):
        monkeypatch.setattr(paths, 'PACKAGE_DIR', tmp_path / 'pkg')
        (tmp_path / 'pkg').mkdir()
        (tmp_path / 'pkg' / 'lfs-deps-12.0').write_text('{}')
        monkeypatch.setenv(paths.STATE_DIR_ENV, str(tmp_path / 'state'))
        assert paths.db_path() == tmp_path / 'state' / 'lfs-deps-12.0'

    def test_falls_back_to_existing_state_database(self, tmp_path, monkeypatch):
        monkeypatch.setattr(paths, 'PACKAGE_DIR', tmp_path / 'pkg')
        (tmp_path / 'pkg').mkdir()
        state = tmp_path / 'state'
        state.mkdir()
        (state / 'lfs-deps-10.1').write_text('{}')
        monkeypatch.setenv(paths.STATE_DIR_ENV, str(state))
        assert paths.db_path() == state / 'lfs-deps-10.1'

    def test_default_name_when_nothing_exists(self, tmp_path, monkeypatch):
        monkeypatch.setattr(paths, 'PACKAGE_DIR', tmp_path / 'pkg')
        (tmp_path / 'pkg').mkdir()
        monkeypatch.setenv(paths.STATE_DIR_ENV, str(tmp_path / 'state'))
        assert paths.db_path().name == paths.DEFAULT_DB_FILENAME


class TestEnsureDb:
    @pytest.fixture
    def seeded_package(self, tmp_path, monkeypatch):
        package = tmp_path / 'pkg'
        package.mkdir()
        seed = package / 'lfs-deps-11.3'
        seed.write_text('{"A-1.0": {}}')
        monkeypatch.setattr(paths, 'PACKAGE_DIR', package)
        return seed

    def test_seeds_from_the_packaged_copy(self, tmp_path, monkeypatch,
                                          seeded_package, state_and_cache):
        database = paths.ensure_db()
        assert database == state_and_cache[0] / 'lfs-deps-11.3'
        assert database.read_text() == '{"A-1.0": {}}'

    def test_seeding_leaves_the_package_copy_intact(self, seeded_package,
                                                    state_and_cache):
        paths.ensure_db()
        assert seeded_package.exists()

    def test_does_not_overwrite_existing_database(self, seeded_package,
                                                  state_and_cache):
        state = state_and_cache[0]
        state.mkdir()
        (state / 'lfs-deps-11.3').write_text('{"local": {}}')
        assert paths.ensure_db().read_text() == '{"local": {}}'

    def test_is_idempotent(self, seeded_package, state_and_cache):
        first = paths.ensure_db()
        first.write_text('{"edited": {}}')
        assert paths.ensure_db().read_text() == '{"edited": {}}'

    def test_leaves_no_temporary_file(self, seeded_package, state_and_cache):
        database = paths.ensure_db()
        assert not (database.parent / f'{database.name}.seed-tmp').exists()

    def test_seed_false_skips_copying(self, seeded_package, state_and_cache):
        database = paths.ensure_db(seed=False)
        assert not database.exists()

    def test_missing_seed_returns_a_path_to_scrape_into(self, tmp_path,
                                                        monkeypatch,
                                                        state_and_cache):
        package = tmp_path / 'empty-pkg'
        package.mkdir()
        monkeypatch.setattr(paths, 'PACKAGE_DIR', package)
        database = paths.ensure_db()
        assert not database.exists()
        assert database.parent == state_and_cache[0]

    @running_as_root
    def test_raises_when_state_dir_is_unwritable(self, seeded_package,
                                                 unwritable_dir, monkeypatch):
        monkeypatch.setenv(paths.STATE_DIR_ENV, str(unwritable_dir / 'state'))
        with pytest.raises(PermissionError):
            paths.ensure_db()


class TestMigrateLegacyState:
    @pytest.fixture
    def legacy(self, tmp_path):
        """A site-packages-shaped legacy root, with no checkout markers."""
        root = tmp_path / 'site-packages'
        (root / paths.LEGACY_SOURCES_DIRNAME).mkdir(parents=True)
        (root / '.installed_log').write_text('zlib-1.2.13\nopenssl-3.0.8\n')
        (root / 'lfs-deps-11.3').write_text('{"A-1.0": {}}')
        (root / paths.LEGACY_SOURCES_DIRNAME / 'zlib.tar.gz').write_text('tarball')
        return root

    def test_moves_the_install_log(self, legacy, state_and_cache):
        state, _ = state_and_cache
        paths.migrate_legacy_state(legacy_root=legacy)
        assert (state / '.installed_log').read_text().startswith('zlib-1.2.13')
        assert not (legacy / '.installed_log').exists()

    def test_moves_the_database(self, legacy, state_and_cache):
        state, _ = state_and_cache
        paths.migrate_legacy_state(legacy_root=legacy)
        assert (state / 'lfs-deps-11.3').read_text() == '{"A-1.0": {}}'

    def test_moves_cached_tarballs(self, legacy, state_and_cache):
        _, cache = state_and_cache
        paths.migrate_legacy_state(legacy_root=legacy)
        assert (cache / 'zlib.tar.gz').read_text() == 'tarball'

    def test_removes_the_emptied_legacy_source_dir(self, legacy, state_and_cache):
        paths.migrate_legacy_state(legacy_root=legacy)
        assert not (legacy / paths.LEGACY_SOURCES_DIRNAME).exists()

    def test_reports_what_moved(self, legacy, state_and_cache):
        migrated = paths.migrate_legacy_state(legacy_root=legacy)
        assert len(migrated) == 3
        assert all(destination.exists() for _, destination in migrated)

    def test_never_clobbers_existing_state(self, legacy, state_and_cache):
        state, _ = state_and_cache
        state.mkdir()
        (state / '.installed_log').write_text('already-built\n')
        paths.migrate_legacy_state(legacy_root=legacy)
        assert (state / '.installed_log').read_text() == 'already-built\n'
        assert (legacy / '.installed_log').exists(), 'legacy copy must survive'

    def test_migrates_only_the_missing_items(self, legacy, state_and_cache):
        state, _ = state_and_cache
        state.mkdir()
        (state / '.installed_log').write_text('already-built\n')
        migrated = paths.migrate_legacy_state(legacy_root=legacy)
        assert [source.name for source, _ in migrated] == [
            'lfs-deps-11.3', 'zlib.tar.gz']

    def test_is_idempotent(self, legacy, state_and_cache):
        paths.migrate_legacy_state(legacy_root=legacy)
        assert paths.migrate_legacy_state(legacy_root=legacy) == []

    def test_second_run_preserves_content(self, legacy, state_and_cache):
        state, _ = state_and_cache
        paths.migrate_legacy_state(legacy_root=legacy)
        (state / '.installed_log').write_text('zlib-1.2.13\nnew-pkg\n')
        paths.migrate_legacy_state(legacy_root=legacy)
        assert (state / '.installed_log').read_text() == 'zlib-1.2.13\nnew-pkg\n'

    def test_no_legacy_state_is_a_no_op(self, tmp_path, state_and_cache):
        empty = tmp_path / 'clean-site-packages'
        empty.mkdir()
        assert paths.migrate_legacy_state(legacy_root=empty) == []
        assert not state_and_cache[0].exists()

    def test_missing_legacy_root_is_a_no_op(self, tmp_path, state_and_cache):
        assert paths.migrate_legacy_state(legacy_root=tmp_path / 'gone') == []

    def test_creates_the_destination_directories(self, legacy, state_and_cache):
        state, cache = state_and_cache
        paths.migrate_legacy_state(legacy_root=legacy)
        assert state.is_dir() and cache.is_dir()

    def test_source_checkout_is_copied_not_moved(self, legacy, state_and_cache):
        # An editable install points the legacy root at the working tree.
        (legacy / 'setup.py').write_text('# marker')
        state, cache = state_and_cache
        paths.migrate_legacy_state(legacy_root=legacy)
        assert (legacy / '.installed_log').exists()
        assert (legacy / 'lfs-deps-11.3').exists()
        assert (state / '.installed_log').exists()
        assert (cache / 'zlib.tar.gz').exists()

    def test_source_checkout_migration_is_idempotent(self, legacy,
                                                     state_and_cache):
        (legacy / '.git').mkdir()
        paths.migrate_legacy_state(legacy_root=legacy)
        assert paths.migrate_legacy_state(legacy_root=legacy) == []

    def test_moves_nested_source_directories(self, legacy, state_and_cache):
        build = legacy / paths.LEGACY_SOURCES_DIRNAME / 'zlib-1.2.13'
        build.mkdir()
        (build / 'Makefile').write_text('all:')
        _, cache = state_and_cache
        paths.migrate_legacy_state(legacy_root=legacy)
        assert (cache / 'zlib-1.2.13' / 'Makefile').read_text() == 'all:'

    def test_ignores_temporary_database_files(self, legacy, state_and_cache):
        (legacy / 'lfs-deps-11.3.tmp').write_text('truncated')
        state, _ = state_and_cache
        paths.migrate_legacy_state(legacy_root=legacy)
        assert not (state / 'lfs-deps-11.3.tmp').exists()

    def test_defaults_to_the_installed_package_parent(self, tmp_path,
                                                      monkeypatch,
                                                      state_and_cache):
        legacy = tmp_path / 'site-packages'
        legacy.mkdir()
        (legacy / '.installed_log').write_text('zlib-1.2.13\n')
        monkeypatch.setattr(paths, 'LEGACY_ROOT', legacy)
        migrated = paths.migrate_legacy_state()
        assert [destination.parent for _, destination in migrated] == [
            state_and_cache[0]]

    def test_unreadable_entry_does_not_abort_the_rest(self, legacy,
                                                      state_and_cache,
                                                      monkeypatch):
        state, _ = state_and_cache
        real_relocate = paths._relocate

        def flaky(source, destination, copy_only):
            if source.name == '.installed_log':
                raise OSError('device or resource busy')
            return real_relocate(source, destination, copy_only)

        monkeypatch.setattr(paths, '_relocate', flaky)
        migrated = paths.migrate_legacy_state(legacy_root=legacy)
        assert [source.name for source, _ in migrated] == [
            'lfs-deps-11.3', 'zlib.tar.gz']
        assert (state / 'lfs-deps-11.3').exists()


class TestPreflight:
    def test_creates_both_directories(self, state_and_cache):
        state, cache = state_and_cache
        assert paths.preflight(migrate=False) == (state, cache)
        assert state.is_dir() and cache.is_dir()

    def test_can_skip_the_source_cache(self, state_and_cache):
        state, cache = state_and_cache
        assert paths.preflight(sources=False, migrate=False) == (state, None)
        assert not cache.exists()

    def test_runs_the_migration(self, tmp_path, monkeypatch, state_and_cache):
        legacy = tmp_path / 'site-packages'
        legacy.mkdir()
        (legacy / '.installed_log').write_text('zlib-1.2.13\n')
        monkeypatch.setattr(paths, 'LEGACY_ROOT', legacy)
        paths.preflight()
        assert (state_and_cache[0] / '.installed_log').exists()

    @running_as_root
    def test_fails_before_expensive_work(self, unwritable_dir, monkeypatch):
        # The whole point: this must raise instead of ~1600 scraped pages
        # discovering the problem at the final write.
        monkeypatch.setenv(paths.STATE_DIR_ENV, str(unwritable_dir / 'state'))
        with pytest.raises(PermissionError):
            paths.preflight()

    @running_as_root
    def test_reports_the_unwritable_cache_directory(self, tmp_path,
                                                    unwritable_dir,
                                                    monkeypatch):
        monkeypatch.setenv(paths.STATE_DIR_ENV, str(tmp_path / 'state'))
        monkeypatch.setenv(paths.CACHE_DIR_ENV, str(unwritable_dir / 'srcs'))
        with pytest.raises(PermissionError, match='srcs'):
            paths.preflight()
