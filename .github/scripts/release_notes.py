"""Append direct commits without changing GitHub's generated release notes."""

import html
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import quote, unquote, urlsplit


def command(*args: str) -> str:
    return subprocess.run(args, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True).stdout


def previous_tag(notes: str, server: str, repository: str, current: str) -> str | None:
    # The footer is GitHub's own decision, including its prerelease selection.
    lines = [line for line in notes.splitlines() if line.startswith('**Full Changelog**: ')]
    if not lines:
        if '**Full Changelog**' in notes:
            raise ValueError('Malformed Full Changelog footer')
        pages = json.loads(command('gh', 'api', '--method', 'GET', '--paginate', '--slurp', f'repos/{repository}/releases', '-F', 'per_page=100'))
        if not isinstance(pages, list) or not pages or any(not isinstance(page, list) for page in pages):
            raise ValueError('Unexpected releases response')
        for release in (release for page in pages for release in page):
            if not isinstance(release, dict) or not isinstance(release.get('tag_name'), str) or not isinstance(release.get('draft'), bool):
                raise ValueError('Unexpected release response')
            if not release['draft'] and release['tag_name'] != current:
                raise ValueError('Cannot determine previous release from generated notes')
        return None
    if len(lines) != 1:
        raise ValueError('Generated notes have no unambiguous Full Changelog footer')
    url = urlsplit(lines[0].removeprefix('**Full Changelog**: '))
    origin = urlsplit(server)
    if (url.scheme, url.netloc) != (origin.scheme, origin.netloc) or url.query or url.fragment:
        raise ValueError('Unexpected Full Changelog URL')
    prefix = origin.path.rstrip('/') + '/' + repository + '/'
    if not url.path.startswith(prefix):
        raise ValueError('Unexpected Full Changelog repository')
    path = url.path[len(prefix):]
    if path.startswith('commits/') and unquote(path[len('commits/'):]) == current:
        return None
    if path.startswith('compare/'):
        parts = path[len('compare/'):].split('...')
        if len(parts) == 2 and unquote(parts[1]) == current and parts[0]:
            return unquote(parts[0])
    raise ValueError('Unexpected Full Changelog range')


def ancestor(older: str, newer: str) -> bool:
    result = subprocess.run(['git', 'merge-base', '--is-ancestor', older, newer], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if result.returncode not in (0, 1):
        raise ValueError('Cannot validate commit ancestry')
    return result.returncode == 0


def direct_commits(repository: str, current: str, previous: str | None) -> list[tuple[str, str]]:
    if command('git', 'rev-parse', '--is-shallow-repository').strip() != 'false':
        raise ValueError('A complete checkout is required')
    head = command('git', 'rev-parse', '--verify', f'refs/tags/{current}^{{commit}}').strip()
    if head != command('git', 'rev-parse', 'HEAD').strip():
        raise ValueError('Checkout does not match the release tag')
    revision = head
    if previous is not None:
        command('git', 'check-ref-format', f'refs/tags/{previous}')
        base = command('git', 'rev-parse', '--verify', f'refs/tags/{previous}^{{commit}}').strip()
        if not ancestor(base, head):
            raise ValueError('Previous release tag is not an ancestor of this release')
        revision = f'{base}..{head}'
    commits = command('git', 'rev-list', '--reverse', revision).splitlines()
    direct = []
    for sha in commits:
        pages = json.loads(command('gh', 'api', '--method', 'GET', '--paginate', '--slurp', f'repos/{repository}/commits/{sha}/pulls', '-F', 'per_page=100'))
        if not isinstance(pages, list) or not pages or any(not isinstance(page, list) for page in pages):
            raise ValueError('Unexpected associated pull requests response')
        in_pull = False
        for pull in (pull for page in pages for pull in page):
            if pull['merged_at'] is None:
                continue
            merge_sha = pull['merge_commit_sha']
            if not isinstance(merge_sha, str) or not re.fullmatch(r'[0-9a-f]{40}', merge_sha):
                raise ValueError('Missing merged pull request commit')
            if ancestor(merge_sha, head):
                in_pull = True
                break
        if not in_pull:
            subject = command('git', 'show', '-s', '--format=%s', sha).strip()
            direct.append((sha, subject))
    return direct


def augment(notes: str, server: str, repository: str, current: str) -> str:
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
        raise ValueError('Invalid repository')
    previous = previous_tag(notes, server, repository, current)
    commits = direct_commits(repository, current, previous)
    if not commits:
        return notes
    lines = []
    for sha, subject in commits:
        # Prevent commit subjects from injecting Markdown, HTML, or extra lines.
        safe_subject = html.escape(' '.join(subject.split()), quote=True)
        safe_subject = re.sub(r'([\\`*_{}\[\]()#+.!|>~-])', r'\\\1', safe_subject)
        url = f'{server.rstrip("/")}/{repository}/commit/{quote(sha, safe="")}'
        lines.append(f'- {safe_subject} ([{sha[:7]}]({url}))')
    separator = '\n' if notes.endswith('\n') else '\n\n'
    return notes + separator + '## Прямые коммиты\n' + '\n'.join(lines) + '\n'


def main() -> None:
    path = Path(sys.argv[1])
    notes = path.read_text(encoding='utf-8')
    result = augment(notes, os.environ['GITHUB_SERVER_URL'], os.environ['GITHUB_REPOSITORY'], os.environ['GITHUB_REF_NAME'])
    path.write_text(result, encoding='utf-8')


if __name__ == '__main__':
    main()
