"""Regression checks for bounds, live counters and optimized Python verification."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from validation import validate_evidence

TOOLS = Path(__file__).resolve().parent


def excerpt(label, representation, text, start=0, end=None):
    end = len(text) if end is None else end
    exact = text[start:end]
    return dict(label=label, representation=representation,
                start_codepoint=start, end_codepoint=end, exact=exact,
                exact_sha256=hashlib.sha256(exact.encode()).hexdigest())


def inventory(root):
    entries = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
               for p in root.rglob('*') if p.is_file() and p.name != 'inventory.sha256.json'}
    (root / 'inventory.sha256.json').write_text(json.dumps(entries))


def fixture(root, speed='2.99792458'):
    gps = root / 'sources/gps'
    snow = root / 'sources/snow'
    gps.mkdir(parents=True)
    snow.mkdir(parents=True)
    text = f'c   Speed of light ({speed}x108 m/sec)'
    (gps / 'example.txt').write_text(text)
    (gps / 'evidence.json').write_text(json.dumps([excerpt('G8', 'example.txt', text)]))
    # Deliberately three excerpts and zero controls, unlike the real 19/4 batch.
    (snow / 'example.txt').write_text('абв😀д')
    entries = [excerpt('S1', 'example.txt', 'абв😀д', 0, 2),
               excerpt('S2', 'example.txt', 'абв😀д', 2, 5)]
    (snow / 'evidence.json').write_text(json.dumps(entries))
    for case, text in [('gps-clock', 'НИНА: Пример.\n\nИЛЬЯ: Ответ.\n'),
                       ('snow-pump', 'РАССКАЗЧИК: Пример.\n')]:
        directory = root / 'evaluator-only' / case
        directory.mkdir(parents=True)
        for stage in ('draft', 'final'):
            (directory / f'{stage}.txt').write_text(text)
    (root / 'evaluator-only/controls.json').write_text('[]')
    inventory(root)


def optimized_verify(root):
    code = ('import sys; from pathlib import Path; '
            f'sys.path.insert(0, {str(TOOLS)!r}); import prepare; '
            f'prepare.ROOT = Path({str(root)!r}).resolve(); prepare.verify()')
    return subprocess.run([sys.executable, '-O', '-c', code], capture_output=True, text=True)


class ValidationTests(unittest.TestCase):
    def test_ranges_and_unicode(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / 'text.txt').write_text('абв😀д')
            self.assertEqual(validate_evidence(folder, [excerpt('E', 'text.txt', 'абв😀д', 3, 4)]), 1)
            # Matching Python slices/hashes cannot legitimize invalid coordinates.
            for start, end in [(-2, 5), (0, 6), (0, 0), (4, 2), (0, -1), (True, 3), ('0', 3)]:
                with self.subTest(start=start, end=end):
                    entry = excerpt('E', 'text.txt', 'абв😀д')
                    entry.update(start_codepoint=start, end_codepoint=end)
                    if type(start) is int and type(end) is int:
                        value = 'абв😀д'[start:end]
                        entry.update(exact=value, exact_sha256=hashlib.sha256(value.encode()).hexdigest())
                    with self.assertRaisesRegex(ValueError, 'Invalid evidence range'):
                        validate_evidence(folder, [entry])

    def test_optimized_verify_counts_actual_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture(root)
            result = optimized_verify(root)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('3 exact excerpts; 0 control recipes; 9 arithmetic/geometry checks', result.stdout)

    def test_optimized_verify_rejects_invalid_bounds(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture(root)
            path = root / 'sources/snow/evidence.json'
            entries = json.loads(path.read_text())
            entries[0] = excerpt('S1', 'example.txt', 'абв😀д', -2, 5)
            path.write_text(json.dumps(entries))
            inventory(root)
            result = optimized_verify(root)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Invalid evidence range', result.stderr)

    def test_optimized_verify_does_not_skip_arithmetic(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture(root, speed='2.99792459')
            result = optimized_verify(root)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Calculation mismatch: c_m_per_second', result.stderr)


if __name__ == '__main__':
    unittest.main()
