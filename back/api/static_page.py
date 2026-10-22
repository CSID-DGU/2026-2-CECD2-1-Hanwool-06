"""화면 빌드와 현재 자료를 한 HTML 파일로 묶는다. 로그인 없이 파일만 열어 보는 저장본이다.

자료가 통째로 들어가므로(청구서·주소 포함) 파일을 공개된 곳에 두지 않는다. 화면은 먼저 `npm run build` 로 만든다.
"""
from __future__ import annotations

import base64
import gzip
import json
import mimetypes
import re
from pathlib import Path

from . import catalog, config

DIST = config.ROOT / 'front/dist'
OPENING = '<script>window.__STATIC_DATA_GZ__="'   # 저장본에만 있는 첫머리: 화면이 자료를 읽는 자리이자 앞서 만든 저장본을 알아보는 표시


def current_payload() -> dict:
    """총괄 관리자가 보는 전체 자료. 화면이 서버의 /api/data 대신 쓴다."""
    return {'version': catalog.data_version(None)} | catalog.payload(None)


def build(destination: Path, payload: dict | None = None, dist: Path = DIST) -> Path:
    index = dist / 'index.html'
    if not index.is_file():
        raise FileNotFoundError('front/dist 가 없습니다. 먼저 npm run build 를 실행하세요.')
    destination = Path(destination).resolve()
    # 서버가 그대로 내보내는 폴더(dist)나 빌드 때 그리로 복사되는 폴더(public)에 두면 로그인 없이 전체 자료가 열린다.
    if any(destination.is_relative_to(folder.resolve()) for folder in (dist, dist.parent / 'public')):
        raise ValueError('화면이 공개되는 폴더(front/dist, front/public)에는 저장할 수 없습니다. 다른 폴더를 고르세요.')
    if destination.is_dir():
        raise ValueError('폴더가 아니라 저장할 파일 이름을 적어 주세요.')
    if destination.exists():
        # 앞서 만든 저장본만 새로 덮어쓴다: HTML 문서로 시작하고 첫머리에 자료 스크립트가 있어야 한다.
        # 그 문자열을 인용만 하는 다른 파일(화면 소스, 이 모듈)은 저장본이 아니다.
        with destination.open('rb') as existing:
            head = existing.read(4096)
        if not (head.lstrip().lower().startswith(b'<!doctype html') and OPENING.encode() in head):
            raise ValueError('같은 이름의 다른 파일이 이미 있습니다. 덮어쓰지 않으니 다른 이름을 쓰세요.')
    html = index.read_text(encoding='utf-8')
    assets = {path.name: path for path in (dist / 'assets').iterdir()}
    public = {path.name: path for path in dist.iterdir() if path.is_file() and path.name != 'index.html'}   # front/public 의 파일

    def data_uri(path):   # 그림·글꼴은 data URI 로 바꿔 파일 하나로 열리게 한다
        mime = mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
        return f'data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}'

    def inline(name, kind):
        text = assets[name].read_text(encoding='utf-8')
        text = re.sub(r'/assets/([\w.-]+)', lambda m: data_uri(assets[m.group(1)]) if assets[m.group(1)].suffix not in ('.js', '.css') else m.group(0), text)
        if public:
            text = re.sub(r'(["\'(])/(' + '|'.join(map(re.escape, public)) + r')(["\')])',
                          lambda m: m.group(1) + data_uri(public[m.group(2)]) + m.group(3), text)
        return text.replace(f'</{kind}', f'<\\/{kind}')

    html = re.sub(r'<script type="module" crossorigin src="/assets/([^"]+)"></script>',
                  lambda m: '<script type="module">' + inline(m.group(1), 'script') + '</script>', html)
    html = re.sub(r'<link rel="stylesheet" crossorigin href="/assets/([^"]+)">',
                  lambda m: '<style>' + inline(m.group(1), 'style') + '</style>', html)
    data = payload if payload is not None else current_payload()
    packed = base64.b64encode(gzip.compress(json.dumps(data, ensure_ascii=False, separators=(',', ':')).encode(), 9)).decode()
    html = html.replace('<script type="module">', f'{OPENING}{packed}";</script>\n    <script type="module">', 1)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(html, encoding='utf-8')
    destination.chmod(0o600)
    return destination
