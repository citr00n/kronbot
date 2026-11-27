import importlib.util
import json
from pathlib import Path
import subprocess

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / '.github/scripts/release_notes.py'
spec = importlib.util.spec_from_file_location('release_notes', SCRIPT)
release_notes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release_notes)
SERVER = 'https://github.com'
REPO = 'citr00n/kronbot'


def git(*args):
    return subprocess.run(['git', *args], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    git('init', '-q')
    git('config', 'user.name', 'Test')
    git('config', 'user.email', 'test@example.invalid')
    def commit(subject):
        git('commit', '--allow-empty', '-qm', subject)
        return git('rev-parse', 'HEAD')
    return commit


def mock_pulls(monkeypatch, pulls, releases=None):
    command = release_notes.command
    def fake(*args):
        if args[0] == 'gh':
            assert args[1:4] == ('api', '--method', 'GET')
            if args[6].endswith('/releases'):
                return json.dumps(releases if releases is not None else [[]])
            sha = args[6].split('/')[-2]
            return json.dumps(pulls.get(sha, [[]]))
        return command(*args)
    monkeypatch.setattr(release_notes, 'command', fake)


def test_real_notes_preserved_and_range_excludes_pr(repo, monkeypatch):
    old = repo('initial commit')
    git('tag', 'v0.3.0')
    direct = repo('fix: restore forecast')
    pr = repo('Feature from PR')
    git('tag', 'v0.3.1')
    mock_pulls(monkeypatch, {pr: [[], [{'merged_at': '2026-01-01', 'merge_commit_sha': pr}]]})
    notes = ("## What's Changed\n* Feature from PR by @user in https://github.com/citr00n/kronbot/pull/5\n\n"
             "**Full Changelog**: https://github.com/citr00n/kronbot/compare/v0.3.0...v0.3.1\n")
    result = release_notes.augment(notes, SERVER, REPO, 'v0.3.1')
    assert result.startswith(notes)
    assert 'fix: restore forecast' in result
    assert direct in result
    assert old not in result
    assert result.count('Feature from PR') == 1


def test_first_prerelease_and_markdown_escaping(repo, monkeypatch):
    first = repo('initial setup')
    second = repo('<script> [unsafe](https://example.invalid)')
    git('tag', 'v1.0.0a1')
    mock_pulls(monkeypatch, {})
    notes = "**Full Changelog**: https://github.com/citr00n/kronbot/commits/v1.0.0a1\n"
    result = release_notes.augment(notes, SERVER, REPO, 'v1.0.0a1')
    assert result.startswith(notes)
    assert first in result and second in result
    assert '&lt;script&gt;' in result and r'\[unsafe\]' in result


def test_prerelease_uses_github_footer_not_latest_tag(repo, monkeypatch):
    repo('initial')
    git('tag', 'v1.0.0')
    expected = repo('since stable')
    git('tag', 'v1.1.0a1')
    repo('since alpha')
    git('tag', 'v1.1.0a2')
    mock_pulls(monkeypatch, {})
    notes = '**Full Changelog**: https://github.com/citr00n/kronbot/compare/v1.0.0...v1.1.0a2\n'
    assert expected in release_notes.augment(notes, SERVER, REPO, 'v1.1.0a2')


def test_direct_merge_is_included(repo, monkeypatch):
    repo('base')
    git('tag', 'v1.0.0')
    git('checkout', '-qb', 'feature')
    repo('feature commit')
    git('checkout', '-q', '-')
    repo('main commit')
    git('merge', '--no-ff', '-qm', 'direct merge', 'feature')
    merge = git('rev-parse', 'HEAD')
    git('tag', 'v1.0.1')
    mock_pulls(monkeypatch, {})
    notes = '**Full Changelog**: https://github.com/citr00n/kronbot/compare/v1.0.0...v1.0.1\n'
    assert merge in release_notes.augment(notes, SERVER, REPO, 'v1.0.1')




def test_only_prs_leaves_notes_unchanged(repo, monkeypatch):
    repo('base')
    git('tag', 'v1.0.0')
    pr = repo('PR')
    git('tag', 'v1.0.1')
    mock_pulls(monkeypatch, {pr: [[{'merged_at': '2026-01-01', 'merge_commit_sha': pr}]]})
    notes = '**Full Changelog**: https://github.com/citr00n/kronbot/compare/v1.0.0...v1.0.1\n\n'
    assert release_notes.augment(notes, SERVER, REPO, 'v1.0.1') == notes


def test_first_release_without_footer(repo, monkeypatch):
    initial = repo('initial setup')
    git('tag', 'v1.0.0a1')
    mock_pulls(monkeypatch, {}, [[{'tag_name': 'v0.1.0', 'draft': True}]])
    notes = "## What's Changed\n* A pull request\n"
    result = release_notes.augment(notes, SERVER, REPO, 'v1.0.0a1')
    assert result.startswith(notes)
    assert initial in result


def test_missing_footer_with_published_release_fails_closed(repo, monkeypatch):
    repo('initial')
    git('tag', 'v1.0.0')
    mock_pulls(monkeypatch, {}, [[{'tag_name': 'v0.9.0', 'draft': False}]])
    with pytest.raises(ValueError, match='Cannot determine previous'):
        release_notes.augment("## What's Changed\n", SERVER, REPO, 'v1.0.0')


def test_pr_merged_elsewhere_does_not_hide_direct_commit(repo, monkeypatch):
    repo('base')
    git('tag', 'v1.0.0')
    git('checkout', '-qb', 'other')
    other_merge = repo('merge on other branch')
    git('checkout', '-q', '-')
    direct = repo('direct change')
    git('tag', 'v1.0.1')
    mock_pulls(monkeypatch, {direct: [[{'merged_at': '2026-01-01', 'merge_commit_sha': other_merge}]]})
    notes = '**Full Changelog**: https://github.com/citr00n/kronbot/compare/v1.0.0...v1.0.1\n'
    assert direct in release_notes.augment(notes, SERVER, REPO, 'v1.0.1')


@pytest.mark.parametrize('notes', [
    '**Full Changelog**: https://github.com/citr00n/kronbot/compare/v0.1.0...v9.0.0',
    '**Full Changelog**: https://evil.invalid/citr00n/kronbot/commits/v1.0.0',
    '**Full Changelog**: https://github.com/citr00n/kronbot/commits/v1.0.0\n' * 2,
])
def test_missing_or_invalid_footer_fails_closed(notes):
    with pytest.raises(ValueError):
        release_notes.previous_tag(notes, SERVER, REPO, 'v1.0.0')


def test_unrelated_previous_tag_fails_closed(repo, monkeypatch):
    repo('root')
    git('tag', 'v1.0.0')
    git('checkout', '--orphan', 'other')
    repo('unrelated')
    git('tag', 'v2.0.0')
    mock_pulls(monkeypatch, {})
    notes = '**Full Changelog**: https://github.com/citr00n/kronbot/compare/v1.0.0...v2.0.0'
    with pytest.raises(ValueError, match='not an ancestor'):
        release_notes.augment(notes, SERVER, REPO, 'v2.0.0')
