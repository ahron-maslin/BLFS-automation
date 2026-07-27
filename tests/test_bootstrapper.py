"""Parser tests for the BLFS book scraper.

The scraper is the schema: nothing downstream can be more correct than what
``collect_package_info()`` extracts from the book's HTML. These tests pin its
behaviour against hand-written fixtures that mimic the real book markup, so a
BLFS layout change fails here instead of silently degrading the database.

Everything is offline. ``bootstrap()`` is exercised with ``url_get`` replaced,
and no test performs network I/O.

Realistic whole-page structures live in ``tests/fixtures/*.html``; narrow parser
quirks are expressed as inline HTML so the input and the assertion stay next to
each other.
"""

import json
from pathlib import Path

import pytest
from bs4 import BeautifulSoup as bs4

from blfs_manager import bootstrapper
from blfs_manager.bootstrapper import (
    collect_package_info, filter_ftp, strip_text,
)
from blfs_manager.define import DbTypes

FIXTURES = Path(__file__).parent / 'fixtures'


def load_fixture(name):
    """Parses a fixture file into a BeautifulSoup document.

    Args:
        name (str): File name inside ``tests/fixtures``.

    Returns:
        bs4.BeautifulSoup: The parsed document.
    """
    return bs4((FIXTURES / name).read_text(), 'html.parser')


def parse(name, element_class='sect1', element='h1'):
    """Runs the collector over a fixture and returns the resulting database.

    Args:
        name (str): Fixture file name.
        element_class (str): Class of the heading element to read the name from.
        element (str): Tag name of the heading element.

    Returns:
        dict: A copy of the module-level database after collection.
    """
    collect_package_info(load_fixture(name), element_class, element)
    return dict(bootstrapper.database)


def parse_html(markup, element_class='sect1', element='h1'):
    """Runs the collector over an inline HTML snippet.

    Args:
        markup (str): HTML source.
        element_class (str): Class of the heading element.
        element (str): Tag name of the heading element.

    Returns:
        dict: A copy of the module-level database after collection.
    """
    collect_package_info(bs4(markup, 'html.parser'), element_class, element)
    return dict(bootstrapper.database)


@pytest.fixture(autouse=True)
def clean_database():
    """Isolates tests from the scraper's module-level ``database`` global."""
    bootstrapper.database.clear()
    yield bootstrapper.database
    bootstrapper.database.clear()


class TestStripText:
    def test_collapses_newline_and_indent(self):
        assert strip_text('Setting up the\n    Xorg Build\n    Environment') == \
            'Setting up the Xorg Build Environment'

    def test_collapses_a_run_of_blank_lines(self):
        assert strip_text('Foo\n\n\nBar') == 'Foo Bar'

    def test_bare_newline_is_not_collapsed(self):
        # The pattern is '\n\s+', so a newline with nothing after it survives.
        # Build commands rely on this: their embedded newlines must be kept.
        assert strip_text('make\nmake install') == 'make\nmake install'

    def test_does_not_trim_leading_or_trailing_spaces(self):
        # This is why 19 external packages ship under keys like ' Tk'.
        assert strip_text('  Tk  ') == '  Tk  '

    def test_leaves_plain_text_untouched(self):
        assert strip_text('NSPR-4.35') == 'NSPR-4.35'

    def test_handles_empty_string(self):
        assert strip_text('') == ''

    def test_non_breaking_space_is_preserved(self):
        # Book chapter cross-references carry U+00A0 and reach the database
        # with it intact, e.g. 'Chapter\xa024.\xa0Graphical Environments'.
        assert strip_text('Chapter\xa024.\xa0Graphical Environments') == \
            'Chapter\xa024.\xa0Graphical Environments'


class TestFilterFtp:
    """Pins the current behaviour of the index-parity mirror filter.

    ``filter_ftp()`` keeps a URL when its position is even *or* it is not an
    FTP URL. That is a positional guess about how the book orders mirrors, not
    a rule about the URLs themselves; these tests document what it actually
    does today so a future rewrite has a baseline to diff against.
    """

    def test_empty_list(self):
        assert filter_ftp([]) == []

    def test_single_http_url_is_kept(self):
        assert filter_ftp(['https://a/x.tar.gz']) == ['https://a/x.tar.gz']

    def test_drops_the_ftp_mirror_following_an_http_url(self):
        assert filter_ftp(['https://a/x.tar.gz', 'ftp://a/x.tar.gz']) == \
            ['https://a/x.tar.gz']

    def test_keeps_every_http_url(self):
        urls = ['https://a/x.tar.gz', 'https://b/x.patch', 'https://c/y.patch']
        assert filter_ftp(urls) == urls

    def test_patch_url_after_the_tarball_survives(self):
        # The 89-package patch case must not be filtered away.
        urls = ['https://a/httpd.tar.bz2', 'https://lfs/httpd-layout.patch']
        assert filter_ftp(urls) == urls

    def test_ftp_url_at_an_even_index_is_kept(self):
        # The heuristic leaks: parity, not the scheme, decides the outcome.
        urls = ['https://a/x.tar.gz', 'ftp://b/x.tar.gz', 'ftp://c/x.tar.gz']
        assert filter_ftp(urls) == ['https://a/x.tar.gz', 'ftp://c/x.tar.gz']

    def test_ftp_only_list_keeps_the_even_entries(self):
        urls = ['ftp://a/x.tar.gz', 'ftp://b/x.tar.gz']
        assert filter_ftp(urls) == ['ftp://a/x.tar.gz']

    def test_leading_ftp_url_is_kept_because_index_zero_is_even(self):
        assert filter_ftp(['ftp://a/x.tar.gz', 'https://b/x.tar.gz']) == \
            ['ftp://a/x.tar.gz', 'https://b/x.tar.gz']

    def test_texlive_bypasses_the_filter_entirely(self):
        urls = ['ftp://tug.org/texlive/a.tar.xz', 'ftp://tug.org/texlive/b.tar.xz']
        assert filter_ftp(urls) == urls

    def test_texlive_returns_the_caller_s_list_object(self):
        # Not a copy: the caller's list is aliased into the database.
        urls = ['ftp://tug.org/texlive/a.tar.xz']
        assert filter_ftp(urls) is urls

    def test_texlive_match_anywhere_returns_all_earlier_urls_too(self):
        # The check runs inside the loop, so a texlive URL at any position
        # discards the partially built result and returns the whole input.
        urls = ['https://a/x.tar.gz', 'ftp://b/x.tar.gz',
                'ftp://tug.org/texlive/c.tar.xz']
        assert filter_ftp(urls) == urls

    def test_matches_texlive_as_a_bare_substring(self):
        # Unanchored: any URL merely containing the word is treated as texlive.
        urls = ['https://example.org/not-texlive-really/a.tar.gz',
                'ftp://example.org/mirror/a.tar.gz']
        assert filter_ftp(urls) == urls


class TestStandardPackagePage:
    def test_extracts_the_package_name_from_h1(self):
        db = parse('standard_package.html')
        assert 'NSPR-4.35' in db

    def test_records_one_entry_per_blfs_package(self):
        db = parse('standard_package.html')
        blfs = [k for k, v in db.items() if v[DbTypes.TYPE] == 'BLFS']
        assert blfs == ['NSPR-4.35']

    def test_extracts_the_download_url(self):
        entry = parse('standard_package.html')['NSPR-4.35']
        assert entry[DbTypes.URL] == [
            'https://archive.mozilla.org/pub/nspr/releases/v4.35/src/nspr-4.35.tar.gz']

    def test_ftp_mirror_is_filtered_out(self):
        entry = parse('standard_package.html')['NSPR-4.35']
        assert not any(u.startswith('ftp://') for u in entry[DbTypes.URL])

    def test_extracts_the_md5_sum(self):
        entry = parse('standard_package.html')['NSPR-4.35']
        assert entry[DbTypes.HASHES] == ['5be51b4a3607b0c2a3b6a0e1b1a1e7ad']

    def test_download_size_lines_are_not_mistaken_for_hashes(self):
        entry = parse('standard_package.html')['NSPR-4.35']
        assert len(entry[DbTypes.HASHES]) == 1

    def test_extracts_required_dependencies(self):
        entry = parse('standard_package.html')['NSPR-4.35']
        assert entry[DbTypes.DEPS][DbTypes.REQUIRED] == \
            ['Zlib-1.2.13', 'libffi-3.4.4']

    def test_extracts_recommended_dependencies(self):
        entry = parse('standard_package.html')['NSPR-4.35']
        assert entry[DbTypes.DEPS][DbTypes.RECOMMENDED] == ['SQLite-3.40.1']

    def test_extracts_optional_dependencies_of_both_kinds(self):
        entry = parse('standard_package.html')['NSPR-4.35']
        assert entry[DbTypes.DEPS][DbTypes.OPTIONAL] == \
            ['Doxygen-1.9.6', 'Cyrus SASL', 'Electric Fence']

    def test_blfs_dependencies_come_from_the_xref_title_not_the_link_text(self):
        markup = ('<div class="sect1"><h1 class="sect1">P-1.0</h1>'
                  '<p class="required">Required: <a class="xref" '
                  'href="../x.html" title="Zlib-1.2.13">these words</a></p></div>')
        entry = parse_html(markup)['P-1.0']
        assert entry[DbTypes.DEPS][DbTypes.REQUIRED] == ['Zlib-1.2.13']

    def test_xref_without_a_title_is_ignored(self):
        markup = ('<div class="sect1"><h1 class="sect1">P-1.0</h1>'
                  '<p class="required">Required: <a class="xref" '
                  'href="../x.html">Zlib-1.2.13</a></p></div>')
        entry = parse_html(markup)['P-1.0']
        assert entry[DbTypes.DEPS][DbTypes.REQUIRED] == []

    def test_extracts_build_commands_in_document_order(self):
        entry = parse('standard_package.html')['NSPR-4.35']
        assert len(entry[DbTypes.COMMANDS]) == 2
        assert entry[DbTypes.COMMANDS][0].startswith('cd nspr &&')
        assert entry[DbTypes.COMMANDS][1] == 'make install'

    def test_command_newlines_and_entities_are_preserved(self):
        entry = parse('standard_package.html')['NSPR-4.35']
        assert '&&\n' in entry[DbTypes.COMMANDS][0]
        assert '&amp;' not in entry[DbTypes.COMMANDS][0]

    def test_extracts_every_kernel_config_block(self):
        entry = parse('standard_package.html')['NSPR-4.35']
        assert len(entry[DbTypes.KCONF]) == 2
        assert '[CONFIG_SND]' in entry[DbTypes.KCONF][0]
        assert '[CONFIG_EXT2_FS]' in entry[DbTypes.KCONF][1]

    def test_marks_the_page_package_as_blfs(self):
        entry = parse('standard_package.html')['NSPR-4.35']
        assert entry[DbTypes.TYPE] == 'BLFS'

    def test_stored_name_matches_the_database_key(self):
        db = parse('standard_package.html')
        assert all(k == v[DbTypes.NAME] for k, v in db.items())

    def test_entry_has_the_full_schema(self):
        entry = parse('standard_package.html')['NSPR-4.35']
        assert set(entry) == {
            DbTypes.NAME, DbTypes.URL, DbTypes.DEPS, DbTypes.COMMANDS,
            DbTypes.HASHES, DbTypes.KCONF, DbTypes.TYPE,
        }


class TestExternalDependencies:
    def test_external_deps_become_their_own_records(self):
        db = parse('standard_package.html')
        assert db['Cyrus SASL'][DbTypes.TYPE] == 'external'
        assert db['Electric Fence'][DbTypes.TYPE] == 'external'

    def test_external_record_is_keyed_on_the_link_text(self):
        db = parse('standard_package.html')
        assert db['Cyrus SASL'][DbTypes.NAME] == 'Cyrus SASL'

    def test_external_record_stores_the_href_as_its_url(self):
        db = parse('standard_package.html')
        assert db['Cyrus SASL'][DbTypes.URL] == ['https://cyrusimap.org/sasl/']

    def test_external_record_has_a_null_hash_placeholder(self):
        db = parse('standard_package.html')
        assert db['Cyrus SASL'][DbTypes.HASHES] == [None]

    def test_external_record_has_no_deps_or_commands(self):
        entry = parse('standard_package.html')['Cyrus SASL']
        assert entry[DbTypes.COMMANDS] == []
        assert entry[DbTypes.KCONF] == []
        assert all(v == [] for v in entry[DbTypes.DEPS].values())

    def test_a_later_page_overwrites_an_earlier_external_record(self):
        # Externals are keyed on anchor text, so the last page scraped wins.
        first = ('<div class="sect1"><h1 class="sect1">A-1.0</h1>'
                 '<p class="optional">Optional: <a class="ulink" '
                 'href="https://one.example/">Shared</a></p></div>')
        second = ('<div class="sect1"><h1 class="sect1">B-1.0</h1>'
                  '<p class="optional">Optional: <a class="ulink" '
                  'href="https://two.example/">Shared</a></p></div>')
        parse_html(first)
        db = parse_html(second)
        assert db['Shared'][DbTypes.URL] == ['https://two.example/']

    def test_wrapped_anchor_text_yields_an_untrimmed_key(self):
        # Reproduces the real defect behind keys such as ' Tk' and ' emacs':
        # strip_text() collapses the wrap but never trims the leading space.
        db = parse('wrapped_name_package.html')
        assert ' Cantarell fonts' in db
        assert 'Cantarell fonts' not in db

    def test_the_untrimmed_name_is_also_what_gets_recorded_as_the_dependency(self):
        db = parse('wrapped_name_package.html')
        entry = db['Setting up the Xorg Build Environment']
        assert entry[DbTypes.DEPS][DbTypes.OPTIONAL] == [' Cantarell fonts']

    def test_package_heading_is_trimmed_even_when_wrapped(self):
        # The page name gets an explicit .strip(); external deps do not.
        db = parse('wrapped_name_package.html')
        assert 'Setting up the Xorg Build Environment' in db

    def test_ulink_inside_itemizedlist_is_a_download_not_an_external_package(self):
        db = parse('standard_package.html')
        assert 'https://archive.mozilla.org/pub/nspr/releases/v4.35/src/' \
               'nspr-4.35.tar.gz' not in db


class TestPackagesWithoutUrlsOrDeps:
    def test_page_without_an_itemizedlist_has_no_urls(self):
        entry = parse('no_urls_package.html')['Introduction to Xorg-7']
        assert entry[DbTypes.URL] == []

    def test_page_without_an_itemizedlist_has_no_hashes(self):
        entry = parse('no_urls_package.html')['Introduction to Xorg-7']
        assert entry[DbTypes.HASHES] == []

    def test_page_without_urls_still_records_deps_and_commands(self):
        entry = parse('no_urls_package.html')['Introduction to Xorg-7']
        assert entry[DbTypes.DEPS][DbTypes.REQUIRED] == ['util-macros-1.19.3']
        assert entry[DbTypes.COMMANDS] == ['export XORG_PREFIX="/usr"']

    def test_package_with_no_dependency_paragraphs(self):
        entry = parse('no_deps_package.html')['File::Which-1.27']
        assert entry[DbTypes.DEPS] == {
            DbTypes.REQUIRED: [], DbTypes.RECOMMENDED: [], DbTypes.OPTIONAL: [],
        }

    def test_package_with_no_dependencies_creates_no_external_records(self):
        db = parse('no_deps_package.html')
        assert list(db) == ['File::Which-1.27']

    def test_page_with_nothing_but_a_heading(self):
        db = parse_html('<div class="sect1"><h1 class="sect1">Bare-1.0</h1></div>')
        entry = db['Bare-1.0']
        assert entry[DbTypes.URL] == []
        assert entry[DbTypes.HASHES] == []
        assert entry[DbTypes.COMMANDS] == []
        assert entry[DbTypes.KCONF] == []

    def test_missing_heading_raises_rather_than_storing_a_bad_name(self):
        # A book heading change must fail loudly, not write a junk key.
        with pytest.raises(AttributeError):
            parse_html('<div class="sect1"><p>no heading here</p></div>')


class TestUrlAndHashPairing:
    def test_patch_url_is_kept_but_carries_no_hash(self):
        entry = parse('patched_package.html')['Apache-2.4.55']
        assert len(entry[DbTypes.URL]) == 2
        assert len(entry[DbTypes.HASHES]) == 1

    def test_the_tarball_is_first_and_the_patch_second(self):
        entry = parse('patched_package.html')['Apache-2.4.55']
        assert entry[DbTypes.URL][0].endswith('.tar.bz2')
        assert entry[DbTypes.URL][1].endswith('.patch')

    def test_hashes_are_gathered_from_every_itemizedlist_on_the_page(self):
        markup = ('<div class="sect1"><h1 class="sect1">P-1.0</h1>'
                  '<div class="itemizedlist"><p>'
                  '<a class="ulink" href="https://a/p.tar.gz">a</a></p>'
                  '<p>Download MD5 sum: ' + 'a' * 32 + '</p></div>'
                  '<div class="itemizedlist"><p>'
                  '<a class="ulink" href="https://b/extra.tar.gz">b</a></p>'
                  '<p>Download MD5 sum: ' + 'b' * 32 + '</p></div></div>')
        entry = parse_html(markup)['P-1.0']
        assert entry[DbTypes.HASHES] == ['a' * 32, 'b' * 32]
        assert len(entry[DbTypes.URL]) == 2

    def test_prose_explaining_no_md5_is_available_yields_no_hash(self):
        # install-tl-unx's book entry reads "not available, because the
        # upstream tarball is regenerated frequently" - no hex MD5 present.
        entry = parse('junk_hash_package.html')['install-tl-unx']
        assert entry[DbTypes.HASHES] == [None]

    def test_prose_after_the_md5_label_yields_no_hash(self):
        markup = ('<div class="sect1"><h1 class="sect1">P-1.0</h1>'
                  '<div class="itemizedlist">'
                  '<p>Download MD5 sum: see upstream</p></div></div>')
        entry = parse_html(markup)['P-1.0']
        assert entry[DbTypes.HASHES] == [None]

    def test_empty_md5_paragraph_yields_no_hash(self):
        markup = ('<div class="sect1"><h1 class="sect1">P-1.0</h1>'
                  '<div class="itemizedlist">'
                  '<p>Download MD5 sum:</p></div></div>')
        entry = parse_html(markup)['P-1.0']
        assert entry[DbTypes.HASHES] == [None]

    def test_md5_is_extracted_even_amid_surrounding_prose(self):
        markup = ('<div class="sect1"><h1 class="sect1">P-1.0</h1>'
                  '<div class="itemizedlist">'
                  '<p>Download MD5 sum: ' + 'C' * 32 + ' (verified upstream)'
                  '</p></div></div>')
        entry = parse_html(markup)['P-1.0']
        assert entry[DbTypes.HASHES] == ['c' * 32]

    def test_url_without_any_md5_paragraph(self):
        markup = ('<div class="sect1"><h1 class="sect1">P-1.0</h1>'
                  '<div class="itemizedlist"><p>Download (HTTP): '
                  '<a class="ulink" href="https://a/p.tar.gz">a</a></p></div></div>')
        entry = parse_html(markup)['P-1.0']
        assert entry[DbTypes.URL] == ['https://a/p.tar.gz']
        assert entry[DbTypes.HASHES] == []


class TestMultiModulePage:
    def _collect_modules(self, name):
        soup = load_fixture(name)
        for module in soup.find_all('div', class_='sect2'):
            if module.find_all('div', class_='package'):
                collect_package_info(module, 'sect2', 'h2')
        return dict(bootstrapper.database)

    def test_each_module_becomes_its_own_package(self):
        db = self._collect_modules('multi_module.html')
        assert set(db) == {'Archive::Zip-1.68', 'File::Which-1.27'}

    def test_the_page_level_h1_is_not_recorded(self):
        db = self._collect_modules('multi_module.html')
        assert 'Perl Modules' not in db

    def test_a_sect2_without_a_package_div_is_skipped(self):
        db = self._collect_modules('multi_module.html')
        assert 'Notes on Perl Modules' not in db
        assert not any('must never be scraped' in c
                       for e in db.values() for c in e[DbTypes.COMMANDS])

    def test_module_urls_and_hashes_are_not_mixed_between_modules(self):
        db = self._collect_modules('multi_module.html')
        assert db['Archive::Zip-1.68'][DbTypes.HASHES] == \
            ['a33993309322164867c99e04a4000ee3']
        assert db['File::Which-1.27'][DbTypes.HASHES] == \
            ['d5c9154262b93398f0750ec364207639']

    def test_module_dependencies_are_scoped_to_their_module(self):
        db = self._collect_modules('multi_module.html')
        assert db['Archive::Zip-1.68'][DbTypes.DEPS][DbTypes.REQUIRED] == ['UnZip-6.0']
        assert db['File::Which-1.27'][DbTypes.DEPS][DbTypes.REQUIRED] == []

    def test_module_commands_are_scoped_to_their_module(self):
        db = self._collect_modules('multi_module.html')
        assert db['Archive::Zip-1.68'][DbTypes.COMMANDS] == \
            ['perl Makefile.PL &&\nmake', 'make install']
        assert db['File::Which-1.27'][DbTypes.COMMANDS] == ['perl Makefile.PL']

    def test_modules_are_recorded_as_blfs_packages(self):
        db = self._collect_modules('multi_module.html')
        assert all(e[DbTypes.TYPE] == 'BLFS' for e in db.values())

    def test_sect1_selectors_would_find_nothing_on_a_module(self):
        soup = load_fixture('multi_module.html')
        module = soup.find_all('div', class_='sect2')[0]
        assert module.find('h1', class_='sect1') is None

    def test_reading_a_module_page_whole_would_collapse_it_into_one_entry(self):
        # Documents why bootstrap() dispatches on the sect2 count: taking the
        # sect1 path here would merge every module's urls, hashes and commands
        # under the chapter heading.
        db = parse('multi_module.html')
        assert set(db) == {'Perl Modules'}
        assert len(db['Perl Modules'][DbTypes.URL]) == 2
        assert len(db['Perl Modules'][DbTypes.HASHES]) == 2


class Response:
    """Stands in for a requests.Response, exposing only what bootstrap() reads."""

    def __init__(self, text):
        self.text = text


def redirect_database(monkeypatch, tmp_path):
    """Points the scraper's database write at a temporary directory.

    Both the supported environment override and the historical module constant
    are set, so the redirection holds however the write path is resolved.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
        tmp_path (pathlib.Path): Directory the database should be written to.

    Returns:
        pathlib.Path: The directory the database will be written to.
    """
    monkeypatch.setenv('BLFS_PM_STATE_DIR', str(tmp_path))
    monkeypatch.setattr(bootstrapper, 'DB_PATH', str(tmp_path / 'lfs-deps-test'),
                        raising=False)
    return tmp_path


def written_databases(state_dir):
    """Lists database files the scraper wrote, excluding temporaries.

    Args:
        state_dir (pathlib.Path): The redirected state directory.

    Returns:
        list: Matching paths, sorted by name.
    """
    return sorted(p for p in state_dir.iterdir()
                  if p.is_file() and not p.name.endswith('.tmp'))


class TestBootstrapDispatch:
    """Drives bootstrap() end to end with the network replaced."""

    @pytest.fixture
    def offline_book(self, monkeypatch, tmp_path):
        pages = {
            'BOOK/longindex.html': 'longindex.html',
            'BOOK/general/nspr.html': 'standard_package.html',
            'BOOK/server/apache.html': 'patched_package.html',
            'BOOK/general/perl-modules.html': 'multi_module.html',
        }
        requested = []

        def fake_url_get(url, headers=None, timeout=30):
            requested.append(url)
            return Response((FIXTURES / pages[url]).read_text())

        monkeypatch.setattr(bootstrapper, 'url_get', fake_url_get)
        state_dir = redirect_database(monkeypatch, tmp_path)
        return state_dir, requested

    def test_scrapes_every_indexed_page(self, offline_book):
        _, requested = offline_book
        bootstrapper.bootstrap('BOOK/')
        assert sorted(requested) == [
            'BOOK/general/nspr.html',
            'BOOK/general/perl-modules.html',
            'BOOK/longindex.html',
            'BOOK/server/apache.html',
        ]

    def test_fragment_links_in_the_index_are_not_fetched(self, offline_book):
        _, requested = offline_book
        bootstrapper.bootstrap('BOOK/')
        assert not any('#' in url for url in requested)

    def test_standard_pages_and_modules_are_both_collected(self, offline_book):
        bootstrapper.bootstrap('BOOK/')
        assert {'NSPR-4.35', 'Apache-2.4.55', 'Archive::Zip-1.68',
                'File::Which-1.27'} <= set(bootstrapper.database)

    def test_module_page_is_not_collapsed_into_its_chapter_heading(self, offline_book):
        bootstrapper.bootstrap('BOOK/')
        assert 'Perl Modules' not in bootstrapper.database

    def test_external_deps_are_written_alongside_blfs_packages(self, offline_book):
        bootstrapper.bootstrap('BOOK/')
        assert bootstrapper.database['Cyrus SASL'][DbTypes.TYPE] == 'external'

    def test_writes_a_loadable_json_database(self, offline_book):
        state_dir, _ = offline_book
        bootstrapper.bootstrap('BOOK/')
        databases = written_databases(state_dir)
        assert len(databases) == 1
        written = json.loads(databases[0].read_text())
        assert written == bootstrapper.database
        assert written['Apache-2.4.55'][DbTypes.URL][1].endswith('.patch')

    def test_leaves_no_temporary_file_behind(self, offline_book):
        state_dir, _ = offline_book
        bootstrapper.bootstrap('BOOK/')
        assert not list(state_dir.glob('*.tmp'))

    def test_a_single_sect2_page_falls_through_to_the_sect1_path(self, monkeypatch,
                                                                 tmp_path):
        # bootstrap() dispatches on 'more than one sect2'. A page carrying
        # exactly one module is read as a standard package instead.
        page = ('<div class="sect1"><h1 class="sect1">Chapter Heading</h1>'
                '<div class="sect2"><h2 class="sect2">Only::Module-1.0</h2>'
                '<div class="package"><div class="itemizedlist"><p>'
                '<a class="ulink" href="https://a/m.tar.gz">a</a></p></div>'
                '</div></div></div>')
        index = ('<h3><a id="package-index"></a>Package Index</h3>\n'
                 '<dl><dt><a href="only.html">Only</a></dt></dl>')

        def fake_url_get(url, headers=None, timeout=30):
            return Response(index if url.endswith('longindex.html') else page)

        monkeypatch.setattr(bootstrapper, 'url_get', fake_url_get)
        redirect_database(monkeypatch, tmp_path)
        bootstrapper.bootstrap('BOOK/')

        assert 'Chapter Heading' in bootstrapper.database
        assert 'Only::Module-1.0' not in bootstrapper.database

    def test_database_global_is_not_reset_between_runs(self, offline_book):
        # bootstrap() accumulates into a module-level dict, so a second scrape
        # of a smaller book keeps every stale entry from the first.
        bootstrapper.bootstrap('BOOK/')
        stale = 'Ghost-9.9'
        bootstrapper.database[stale] = {}
        bootstrapper.bootstrap('BOOK/')
        assert stale in bootstrapper.database
