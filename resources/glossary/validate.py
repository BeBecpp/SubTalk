#!/usr/bin/env python3
"""Validate SubTalk JSON glossary without any external dependencies."""
import json
from pathlib import Path

ROOT=Path(__file__).parent

def load(name):
    return json.loads((ROOT/name).read_text(encoding='utf-8'))

def main():
    books=load('bible_books.json')['books']
    jw=load('jw_terms.json')['items']
    wedding=load('wedding_terms.json')['items']
    names=load('event_names.example.json')['entries']
    policy=load('translation_policy.json')
    sources=load('sources.json')
    eval_cases=load('evaluation_cases.json')['cases']
    assert len(books)==66, f'Expected 66 Bible books, found {len(books)}'
    assert [b['order'] for b in books]==list(range(1,67))
    all_ids=[b['id'] for b in books]+[t['id'] for t in jw+wedding]
    assert len(all_ids)==len(set(all_ids)), 'Duplicate canonical ID'
    assert len({b['tr'] for b in books})==66, 'Duplicate Turkish book name'
    assert len({b['ja'] for b in books})==66, 'Duplicate Japanese book name'
    assert len({b['mn'] for b in books})==66, 'Duplicate Mongolian book name'
    for b in books:
        assert all(isinstance(b[k],str) and b[k].strip() for k in ('mn','ja','tr','id'))
    for t in jw+wedding:
        assert t['status'] in ('tr_attested','review_required')
        assert t['priority'] in ('always','triggered','context')
        assert t['tr'] and t['mn'] and t['ja']
        assert all(s in sources for s in t['source_keys'])
    assert len(eval_cases)>=25
    assert len({c['id'] for c in eval_cases})==len(eval_cases)
    assert all(c['synthetic'] for c in eval_cases)
    assert policy['bible_ref_handling']['full_verse_texts_included'] is False
    assert not any(n['approved'] for n in names), 'Review example people before event'
    print(f'PASS: 66 books; {len(jw)} JW terms; {len(wedding)} wedding terms; {len(names)} example people; {len(sources)} source links; {len(eval_cases)} QA cases')
    print('PASS: IDs, Unicode content, references, statuses, priorities, safe defaults')

if __name__=='__main__':
    main()
