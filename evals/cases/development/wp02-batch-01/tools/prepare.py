"""Offline packaging/checks for WP-02 prose. Not an evaluation or artifact runner."""
import argparse
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import shutil

ROOT = Path(__file__).resolve().parents[1]
CASES = {'gps-clock': 'gps', 'snow-pump': 'snow'}
SPEAKERS = {'gps-clock': {'НИНА', 'ИЛЬЯ'}, 'snow-pump': {'РАССКАЗЧИК'}}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def utterances(case, stage, control=None):
    text = (ROOT / 'evaluator-only' / case / f'{stage}.txt').read_text(encoding='utf-8')
    if control:
        recipes = json.loads((ROOT / 'evaluator-only/controls.json').read_text())
        matches = [r for r in recipes if r['label'] == control]
        if len(matches) != 1:
            raise ValueError('Unknown or ambiguous control')
        recipe = matches[0]
        if recipe['case'] != case or recipe['stage'] != stage:
            raise ValueError('Control belongs to a different case/stage')
        if text.count(recipe['find']) != 1:
            raise ValueError('Exact control selection must occur once')
        text = text.replace(recipe['find'], recipe['replace'], 1)
    result = []
    for paragraph in text.strip().split('\n\n'):
        match = re.fullmatch(r'([А-ЯЁ]+): (.+)', paragraph)
        if not match or match[1] not in SPEAKERS[case]:
            raise ValueError(f'Invalid utterance in {case}/{stage}')
        result.append((match[1], match[2]))
    if {speaker for speaker, _ in result} != SPEAKERS[case]:
        raise ValueError('Missing speaker')
    return result


def verified_path(relative):
    path = ROOT / relative
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(ROOT):
        raise ValueError(f'Unsafe or missing file: {relative}')
    return path


def verify():
    expected = json.loads((ROOT / 'inventory.sha256.json').read_text())
    actual = {p.relative_to(ROOT).as_posix() for p in ROOT.rglob('*')
              if p.is_file() and p.name != 'inventory.sha256.json' and '__pycache__' not in p.parts}
    if actual != set(expected):
        raise ValueError(f'Inventory mismatch; added={actual-set(expected)}, missing={set(expected)-actual}')
    for relative, sha in expected.items():
        if digest(verified_path(relative)) != sha:
            raise ValueError(f'Hash mismatch: {relative}')
    for directory in CASES.values():
        folder = ROOT / 'sources' / directory
        entries = json.loads((folder / 'evidence.json').read_text())
        for entry in entries:
            text = (folder / entry['representation']).read_text(encoding='utf-8')
            exact = text[entry['start_codepoint']:entry['end_codepoint']]
            if exact != entry['exact'] or hashlib.sha256(exact.encode()).hexdigest() != entry['exact_sha256']:
                raise ValueError(f'Evidence mismatch: {entry["label"]}')
    controls = json.loads((ROOT / 'evaluator-only/controls.json').read_text())
    for control in controls:
        utterances(control['case'], control['stage'], control['label'])
    assert Decimal('299792458') * Decimal('0.000001') == Decimal('299.792458')
    assert (7 + 5 - 10) / 2 == 1 and 7 - 1 + 5 - 1 == 10
    assert 61 + 6 + 6 + 10 == 83 and 28 > 20
    for case in CASES:
        for stage in ('draft', 'final'):
            items = utterances(case, stage)
            count = sum(len(text.split()) for _, text in items)
            print(f'{case}/{stage}: {len(items)} utterances, {count} whitespace words; '
                  f'UNMEASURED estimate {count/145:.1f}–{count/115:.1f} min at assumed 115–145 words/min')
    print(f'OK: {len(expected)} file hashes; 19 exact excerpts; 4 control recipes. No quality verdict.')


def export_author(case, output):
    # The complete root inventory and all evaluator files are deliberately absent.
    output.mkdir(parents=True, exist_ok=False)
    public = ROOT / 'author' / case
    sources = ROOT / 'sources' / CASES[case]
    paths = list(public.rglob('*')) + list(sources.rglob('*'))
    for path in paths:
        if path.is_dir():
            continue
        relative = path.relative_to(ROOT)
        source = verified_path(relative)
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    (output / 'README.md').write_text(
        f'# Материалы автора\n\n[Бриф](author/{case}/brief.md) · '
        f'[Досье](author/{case}/dossier.md) · [План](author/{case}/plan.md)\n\n'
        'Модельные исходные материалы development, не профессиональный контроль. '
        'Читайте полные файлы и приложенные источники. Скрипты, сравнения и ответы оценщиков сюда не включены.\n',
        encoding='utf-8')
    # Verify copies byte-for-byte, including the final paragraph of every file.
    for path in paths:
        if path.is_file() and digest(path) != digest(output / path.relative_to(ROOT)):
            raise ValueError('Export copy mismatch')
    if (output / 'evaluator-only').exists():
        raise ValueError('Closed area in author export')
    print(f'Author bundle: {output}')


def export_read(case, stage, blind_id, output, start, stop, control):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,32}', blind_id):
        raise ValueError('Use a neutral 1–32 character blind ID')
    items = utterances(case, stage, control)
    stop = len(items) if stop is None else stop
    if not 1 <= start <= stop <= len(items):
        raise ValueError('Invalid inclusive utterance window')
    output.mkdir(parents=True, exist_ok=False)
    lines = [f'{blind_id}-U{i:03d} | {speaker} | {text}'
             for i, (speaker, text) in enumerate(items, 1) if start <= i <= stop]
    spoken = '\n\n'.join(lines) + '\n'
    (output / 'spoken.txt').write_text(spoken, encoding='utf-8')
    shutil.copyfile(ROOT / 'evaluator-only/task-first-read.md', output / 'task.md')
    # Only the controller sees this binding; do not give stdout to the reader.
    binding = {'blind_id': blind_id, 'case': case, 'stage': stage, 'control': control,
               'base_script_sha256': digest(ROOT / 'evaluator-only' / case / f'{stage}.txt'),
               'spoken_sha256': hashlib.sha256(spoken.encode()).hexdigest(),
               'start_utterance': start, 'stop_utterance': stop,
               'total_utterances': len(items)}
    print(json.dumps(binding, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('verify')
    for command in ('export-author', 'export-first-read'):
        p = sub.add_parser(command)
        p.add_argument('--case', choices=CASES, required=True)
        p.add_argument('--output', type=Path, required=True)
        if command == 'export-first-read':
            p.add_argument('--stage', choices=('draft', 'final'), required=True)
            p.add_argument('--blind-id', required=True)
            p.add_argument('--start', type=int, default=1)
            p.add_argument('--stop', type=int)
            p.add_argument('--control')
    args = parser.parse_args()
    verify()
    if args.command != 'verify':
        output = args.output.expanduser().resolve()
        if output.is_relative_to(ROOT):
            raise ValueError('Export must be outside the frozen batch')
        if args.command == 'export-author':
            export_author(args.case, output)
        else:
            export_read(args.case, args.stage, args.blind_id, output,
                        args.start, args.stop, args.control)


if __name__ == '__main__':
    main()
