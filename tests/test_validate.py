"""Tests for the database validation gate.

Every downstream defect found in review traced back to trusting the scraped
BLFS book data without checking it: 89 packages where url/hash counts
disagree (patches, expected), one hash that is the literal junk string
"frequently", 12 dependency names that are book cross-references rather than
packages, and 2 BLFS packages with no download URL at all. These tests pin
that each class is classified at the right severity, and that CI fails only
on genuine errors.
"""

import json

import pytest

from blfs_manager import paths
from blfs_manager.validate import (
    ERROR, INFO, WARNING,
    Issue, format_report, load_database, summarise, validate_database,
)
from tests.conftest import entry


def issue_codes(issues, severity=None):
    if severity is None:
        return {i.code for i in issues}
    return {i.code for i in issues if i.severity == severity}


class TestSchema:
    def test_clean_database_has_no_errors(self):
        db = {'A-1.0': entry('A-1.0')}
        assert summarise(validate_database(db))[ERROR] == 0

    def test_entry_must_be_a_mapping(self):
        issues = validate_database({'A-1.0': ['not', 'a', 'dict']})
        assert 'not-a-mapping' in issue_codes(issues, ERROR)

    def test_missing_required_key_is_an_error(self):
        db = {'A-1.0': entry('A-1.0')}
        del db['A-1.0']['commands']
        assert 'missing-keys' in issue_codes(validate_database(db), ERROR)

    def test_missing_dep_class_is_an_error(self):
        db = {'A-1.0': entry('A-1.0')}
        del db['A-1.0']['deps']['optional']
        assert 'missing-dep-classes' in issue_codes(validate_database(db), ERROR)

    def test_wrong_pkg_type_is_an_error(self):
        db = {'A-1.0': entry('A-1.0', pkg_type='binary')}
        assert 'bad-pkg-type' in issue_codes(validate_database(db), ERROR)

    @pytest.mark.parametrize('field', ['url', 'commands', 'kconf'])
    def test_non_list_field_is_an_error(self, field):
        db = {'A-1.0': entry('A-1.0')}
        db['A-1.0'][field] = 'not-a-list'
        assert 'bad-field-type' in issue_codes(validate_database(db), ERROR)

    def test_non_string_list_member_is_an_error(self):
        db = {'A-1.0': entry('A-1.0')}
        db['A-1.0']['url'] = [123]
        assert 'bad-field-type' in issue_codes(validate_database(db), ERROR)

    def test_deps_that_is_not_a_mapping_is_an_error(self):
        db = {'A-1.0': entry('A-1.0')}
        db['A-1.0']['deps'] = ['required']
        assert 'bad-field-type' in issue_codes(validate_database(db), ERROR)

    def test_malformed_entry_is_skipped_by_semantic_checks(self):
        # An entry unusable enough to fail schema validation must not also
        # crash _validate_hashes/_validate_urls/_validate_deps.
        db = {'A-1.0': {'name': 'A-1.0'}}
        issues = validate_database(db)
        assert issue_codes(issues, ERROR) == {'missing-keys'}


class TestHashes:
    def test_junk_hash_is_an_error(self):
        # The real database contains exactly this: install-tl-unx's hash is
        # the literal string "frequently".
        db = {'A-1.0': entry('A-1.0', urls=['https://x.invalid/a.tar.gz'],
                             hashes=['frequently'])}
        issues = validate_database(db)
        assert 'bad-hash' in issue_codes(issues, ERROR)

    def test_valid_md5_is_not_flagged(self):
        db = {'A-1.0': entry('A-1.0', urls=['https://x.invalid/a.tar.gz'],
                             hashes=['d41d8cd98f00b204e9800998ecf8427e'])}
        assert 'bad-hash' not in issue_codes(validate_database(db))

    def test_null_hash_is_not_flagged(self):
        db = {'A-1.0': entry('A-1.0', urls=['https://x.invalid/a.tar.gz'],
                             hashes=[None])}
        assert 'bad-hash' not in issue_codes(validate_database(db))

    def test_more_urls_than_hashes_is_informational_not_an_error(self):
        # Patches have no recorded MD5 sum; this is expected, not a defect.
        db = {'A-1.0': entry(
            'A-1.0',
            urls=['https://x.invalid/a.tar.gz', 'https://x.invalid/a.patch'],
            hashes=['d41d8cd98f00b204e9800998ecf8427e'])}
        issues = validate_database(db)
        assert 'url-hash-count-mismatch' in issue_codes(issues, INFO)
        assert issue_codes(issues, ERROR) == set()

    def test_more_hashes_than_urls_is_an_error(self):
        # More hashes than URLs means the pairing itself is broken -- this
        # cannot be explained by unhashed patches.
        db = {'A-1.0': entry(
            'A-1.0', urls=['https://x.invalid/a.tar.gz'],
            hashes=['d41d8cd98f00b204e9800998ecf8427e',
                    'e41d8cd98f00b204e9800998ecf8427e'])}
        assert 'excess-hashes' in issue_codes(validate_database(db), ERROR)


class TestUrls:
    def test_blfs_package_with_no_url_is_a_warning(self):
        db = {'Introduction to Xorg-7': entry('Introduction to Xorg-7', urls=[])}
        issues = validate_database(db)
        assert 'no-url' in issue_codes(issues, WARNING)

    def test_external_package_with_no_url_is_not_flagged(self):
        db = {'A': entry('A', urls=[], pkg_type='external')}
        assert 'no-url' not in issue_codes(validate_database(db))

    def test_relative_url_is_a_warning(self):
        db = {'A-1.0': entry('A-1.0', urls=['../../../lfs/view/network.html'])}
        assert 'unusable-url' in issue_codes(validate_database(db), WARNING)

    def test_duplicate_url_is_a_warning(self):
        url = 'https://x.invalid/a.tar.gz'
        db = {'A-1.0': entry('A-1.0', urls=[url, url])}
        assert 'duplicate-url' in issue_codes(validate_database(db), WARNING)


class TestDependencies:
    def test_dangling_dependency_name_is_a_warning(self):
        # The real database has 12 of these -- book cross-references like
        # "Setting up the Xorg Build Environment" rather than packages.
        db = {'A-1.0': entry('A-1.0', required=['Setting up the Xorg Build Environment'])}
        issues = validate_database(db)
        assert 'unknown-dependency' in issue_codes(issues, WARNING)

    def test_resolvable_dependency_is_not_flagged(self):
        db = {'A-1.0': entry('A-1.0', required=['B-1.0']), 'B-1.0': entry('B-1.0')}
        assert 'unknown-dependency' not in issue_codes(validate_database(db))

    def test_self_dependency_is_a_warning(self):
        db = {'A-1.0': entry('A-1.0', recommended=['A-1.0'])}
        assert 'self-dependency' in issue_codes(validate_database(db), WARNING)

    def test_duplicate_dependency_is_a_warning(self):
        db = {'A-1.0': entry('A-1.0', optional=['B-1.0', 'B-1.0']), 'B-1.0': entry('B-1.0')}
        assert 'duplicate-dependency' in issue_codes(validate_database(db), WARNING)

    def test_unresolved_dependency_lists_referrers(self):
        db = {
            'A-1.0': entry('A-1.0', required=['Ghost']),
            'B-1.0': entry('B-1.0', required=['Ghost']),
        }
        issues = [i for i in validate_database(db) if i.code == 'unknown-dependency']
        assert len(issues) == 1
        assert 'A-1.0' in issues[0].message and 'B-1.0' in issues[0].message


class TestNames:
    def test_untrimmed_key_is_a_warning(self):
        db = {' Adabas': entry('Adabas')}
        assert 'untrimmed-name' in issue_codes(validate_database(db), WARNING)

    def test_empty_key_is_an_error(self):
        db = {'   ': entry('A')}
        assert 'empty-name' in issue_codes(validate_database(db), ERROR)

    def test_key_name_mismatch_is_a_warning(self):
        db = {'A-1.0': entry('Something Else')}
        assert 'key-name-mismatch' in issue_codes(validate_database(db), WARNING)

    def test_case_variant_keys_are_informational(self):
        db = {'Emacs': entry('Emacs'), 'emacs': entry('emacs')}
        issues = validate_database(db)
        assert 'alias-collision' in issue_codes(issues, INFO)


class TestSeverityOrdering:
    def test_issues_are_sorted_errors_first(self):
        db = {
            'A-1.0': entry('A-1.0', urls=['https://x.invalid/a.tar.gz'],
                          hashes=['junk-not-hex']),
            ' B-1.0': entry('B-1.0'),
        }
        issues = validate_database(db)
        severities = [i.severity for i in issues]
        assert severities == sorted(severities, key=[ERROR, WARNING, INFO].index)


class TestSummariseAndReport:
    def test_summarise_counts_every_severity(self):
        issues = [Issue(ERROR, 'x', 'A', 'm'), Issue(WARNING, 'y', 'A', 'm'),
                 Issue(WARNING, 'y', 'B', 'm')]
        counts = summarise(issues)
        assert counts == {ERROR: 1, WARNING: 2, INFO: 0}

    def test_summarise_of_empty_list_is_all_zero(self):
        assert summarise([]) == {ERROR: 0, WARNING: 0, INFO: 0}

    def test_format_report_mentions_source_and_counts(self):
        db = {'A-1.0': entry('A-1.0')}
        report = format_report(validate_database(db), db, source='test.json')
        assert 'test.json' in report
        assert 'OK: no errors.' in report

    def test_format_report_without_errors_reports_ok(self):
        report = format_report([], {}, source=None)
        assert 'OK: no errors.' in report

    def test_format_report_with_errors_omits_ok_line(self):
        issues = [Issue(ERROR, 'bad-hash', 'A-1.0', 'boom')]
        report = format_report(issues, {'A-1.0': entry('A-1.0')}, source=None)
        assert 'OK: no errors.' not in report
        assert 'bad-hash' in report

    def test_format_report_limit_truncates_and_counts_remainder(self):
        issues = [Issue(WARNING, 'no-url', f'pkg-{i}', 'm') for i in range(5)]
        report = format_report(issues, limit=2)
        assert '... and 3 more' in report


class TestLoadDatabase:
    def test_loads_valid_json_object(self, tmp_path):
        path = tmp_path / 'db.json'
        path.write_text(json.dumps({'A-1.0': entry('A-1.0')}))
        assert load_database(str(path)) == {'A-1.0': entry('A-1.0')}

    def test_rejects_non_object_json(self, tmp_path):
        path = tmp_path / 'db.json'
        path.write_text(json.dumps(['not', 'an', 'object']))
        with pytest.raises(ValueError, match='must be a JSON object'):
            load_database(str(path))

    def test_missing_file_raises_oserror(self, tmp_path):
        with pytest.raises(OSError):
            load_database(str(tmp_path / 'nope.json'))


class TestAgainstTheRealBook:
    @pytest.mark.skipif(paths.packaged_db_path() is None,
                        reason='book database not present')
    def test_real_database_has_exactly_the_known_error(self):
        # Confirmed manually during review: the only genuine defect in the
        # shipped 11.3 database is install-tl-unx's junk hash. Every other
        # finding (url/hash count mismatches, dangling cross-references,
        # missing URLs) is expected and must stay below error severity.
        db = load_database(str(paths.packaged_db_path()))
        issues = validate_database(db)
        errors = [i for i in issues if i.severity == ERROR]
        assert len(errors) == 1
        assert errors[0].code == 'bad-hash'
        assert errors[0].package == 'install-tl-unx'

    @pytest.mark.skipif(paths.packaged_db_path() is None,
                        reason='book database not present')
    def test_real_database_surfaces_expected_warnings(self):
        db = load_database(str(paths.packaged_db_path()))
        issues = validate_database(db)
        codes = issue_codes(issues, WARNING)
        assert 'no-url' in codes
        assert 'unknown-dependency' in codes
