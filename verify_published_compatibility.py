"""Read-only regression against immutable, hash-pinned v1/v2 publications."""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile

import market_outlook
from market_outlook_report import render


def main():
    fixture = json.loads((Path(__file__).parent / 'fixtures/legacy-active-reports.json').read_text(encoding='utf-8'))
    repo = fixture['repository']
    if repo != 'cluuacc-gif/ashare-research':
        raise ValueError('unexpected archive repository')
    verified = []
    for case in fixture['cases']:
        tag = case['release_tag']
        if not re.fullmatch(r'market-outlook-\d{4}-\d{2}-\d{2}-\d+-\d+', tag):
            raise ValueError('invalid pinned release tag')
        with tempfile.TemporaryDirectory(prefix='legacy-active-') as temporary:
            directory = Path(temporary)
            subprocess.run(['gh', 'release', 'download', tag, '--repo', repo, '--dir', temporary,
                '--pattern', 'outlook.json', '--pattern', 'report.html', '--pattern', 'source_manifest.json'],
                check=True, capture_output=True, text=True, timeout=120)
            for name in ('outlook.json', 'report.html', 'source_manifest.json'):
                expected = case['files'][name]
                raw = (directory / name).read_bytes()
                if len(raw) != expected['bytes'] or hashlib.sha256(raw).hexdigest() != expected['sha256']:
                    raise ValueError('pinned archive bytes changed: ' + name)
            value = json.loads((directory / 'outlook.json').read_text(encoding='utf-8'))
            source = json.loads((directory / 'source_manifest.json').read_text(encoding='utf-8'))
            if value['active_trade_system']['protocol_sha256'] != case['protocol_sha256']:
                raise ValueError('archived policy identity changed')
            market_outlook.validate(value, source, allow_legacy_active=True)
            if render(value) != (directory / 'report.html').read_text(encoding='utf-8'):
                raise ValueError('legacy HTML no longer reproduces exactly')
            verified.append({'release_tag': tag, 'policy': case['protocol_sha256'],
                'three_source_assets_hash_verified': True, 'original_HTML_unchanged': True})
    print(json.dumps({'legacy_compatibility': verified}, ensure_ascii=True))


if __name__ == '__main__':
    main()
