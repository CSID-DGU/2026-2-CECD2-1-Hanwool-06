"""NCP에 등록된 문서의 작은 어휘 검색. 문서 내용은 지시문이 아닌 인용 자료다."""
from __future__ import annotations

import re
from datetime import date

from .storage import ChatStorageError, decode_json


def _terms(text):
    words = set(re.findall(r'[가-힣a-z0-9]{2,}', text.lower()))
    words -= {'알려줘', '알려주세요', '관련', '대한', '있는', '해주세요', '어떻게', '무엇', '어떤', '경우'}
    grams = {word[i:i + 2] for word in words for i in range(len(word) - 1)}
    return words, grams


def _score(query, text):
    words, grams = _terms(query)
    other_words, other_grams = _terms(text)
    exact = words & other_words
    overlap = grams & other_grams
    if not exact and len(overlap) < 2:
        return 0
    score = (len(exact) * 2 + len(overlap)) / max(1, len(words) * 2 + len(grams))
    return score if score >= .18 else 0


def retrieve(store, query, kind, office_ids, meters):
    raw = store.read('rag/index.json', 128 * 1024, optional=True)
    if raw is None:
        return {'registered': False, 'matches': []}
    index = decode_json(raw)
    docs = index.get('documents') if isinstance(index, dict) else None
    if not isinstance(docs, list) or len(docs) > 24:
        raise ChatStorageError('invalid_corpus_index')
    allowed = {m['id']: m for m in meters}
    matches, registered = [], False
    for doc in docs:
        if not isinstance(doc, dict) or doc.get('kind') != kind:
            continue
        scopes = doc.get('office_ids')
        if scopes is not None and (not isinstance(scopes, list) or not all(isinstance(s, str) for s in scopes)):
            continue
        if kind == 'case' and not scopes:
            continue
        if scopes is not None and office_ids is not None and not set(scopes).intersection(office_ids):
            continue
        key, title = doc.get('key'), doc.get('title')
        suffix = r'manuals/[A-Za-z0-9_./-]+\.md' if kind == 'manual' else r'cases/[A-Za-z0-9_./-]+\.jsonl'
        if not isinstance(key, str) or not re.fullmatch(suffix, key) or '..' in key.split('/') or not isinstance(title, str):
            raise ChatStorageError('invalid_corpus_index')
        registered = True
        payload = store.read('rag/' + key, 512 * 1024, sha256=doc.get('sha256'))
        try:
            body = payload.decode('utf-8-sig')
        except UnicodeError as exc:
            raise ChatStorageError('invalid_document') from exc
        if kind == 'manual':
            # 긴 문서도 실제로 일치한 문단을 인용하므로 앞부분만 잘라 관련 내용을 잃지 않는다.
            chunks = [part for part in re.split(r'\n\s*\n', body) if part.strip()]
            for number, chunk in enumerate(chunks[:1000]):
                for offset in range(0, len(chunk), 1200):
                    text = chunk[offset:offset + 1400]
                    score = _score(query, title + '\n' + text)
                    if score:
                        matches.append({'title': title[:200], 'text': text, 'score': score,
                                        'document': key, 'section': (number, offset), 'kind': 'manual'})
        else:
            lines = body.splitlines()
            if len(lines) > 2000:
                raise ChatStorageError('corpus_too_large')
            for number, line in enumerate(lines):
                if not line.strip():
                    continue
                row = decode_json(line.encode())
                if not isinstance(row, dict) or row.get('verified') is not True:
                    continue
                meter = allowed.get(row.get('meter_id'))
                # 색인의 범위와 사례의 사업소, 현재 계량기 권한이 모두 일치해야 한다.
                if not meter or row.get('office_id') != meter['office_id'] or row['office_id'] not in scopes:
                    continue
                if office_ids is not None and row['office_id'] not in office_ids:
                    continue
                day = row.get('date')
                try:
                    if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day:
                        continue
                except ValueError:
                    continue
                text = row.get('text')
                if not isinstance(text, str) or not text.strip() or not isinstance(row.get('source'), str) or not row['source'].strip():
                    continue
                title = row.get('title') if isinstance(row.get('title'), str) else doc['title']
                score = _score(query, title + '\n' + text)
                if score:
                    matches.append({'title': title[:200], 'text': text[:1400], 'score': score,
                                    'document': key, 'section': number, 'kind': 'case',
                                    'meter_id': meter['id'], 'date': day})
    ranked = sorted(matches, key=lambda row: (-row['score'], row['document'], row['section']))
    return {'registered': registered, 'matches': ranked[:3]}
