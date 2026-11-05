"""Deterministic source-range and arithmetic checks for the local prose package."""
from decimal import Decimal
import hashlib
import re


def validate_evidence(folder, entries):
    labels = set()
    for entry in entries:
        label = entry['label']
        if not isinstance(label, str) or not label or label in labels:
            raise ValueError(f'Invalid or duplicate evidence label: {label!r}')
        labels.add(label)
        path = folder / entry['representation']
        if path.is_symlink() or not path.is_file() or path.resolve().parent != folder.resolve():
            raise ValueError(f'Unsafe evidence representation: {label}')
        text = path.read_text(encoding='utf-8')
        start, end = entry['start_codepoint'], entry['end_codepoint']
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text):
            raise ValueError(f'Invalid evidence range: {label}: [{start!r},{end!r}) / {len(text)}')
        for relative in entry.get('original_page_renders', []):
            render = folder / relative
            if render.is_symlink() or not render.is_file() or not render.resolve().is_relative_to(folder.resolve()):
                raise ValueError(f'Unsafe or missing original page render: {label}: {relative}')
        exact = text[start:end]
        if exact != entry['exact'] or hashlib.sha256(exact.encode()).hexdigest() != entry['exact_sha256']:
            raise ValueError(f'Evidence mismatch: {label}')
    return len(labels)


def require_equal(label, actual, expected):
    if actual != expected:
        raise ValueError(f'Calculation mismatch: {label}: {actual!r} != {expected!r}')


def validate_calculations(gps_entries):
    # G8 has already been checked against the immutable representation.
    speed_entries = [entry for entry in gps_entries if entry['label'] == 'G8']
    if len(speed_entries) != 1:
        raise ValueError('One G8 speed-of-light excerpt is required')
    match = re.search(r'Speed of light \((\d+(?:\.\d+)?)x10(\d+) m/sec\)', speed_entries[0]['exact'])
    if not match:
        raise ValueError('Unsupported G8 speed-of-light notation')
    speed = Decimal(match[1]) * Decimal(10) ** int(match[2])
    road_bias = (Decimal(7) + Decimal(5) - Decimal(10)) / Decimal(2)
    road_distances = (Decimal(7) - road_bias, Decimal(5) - road_bias)
    checks = [
        ('c_m_per_second', speed, Decimal('299792458')),
        ('one_microsecond_m', speed * Decimal('0.000001'), Decimal('299.792458')),
        ('road_bias_km', road_bias, Decimal(1)),
        ('road_corrected_km', road_distances, (Decimal(6), Decimal(4))),
        ('road_total_km', sum(road_distances), Decimal(10)),
        ('snow_interview_total', sum((61, 6, 6, 10)), 83),
        ('snow_sept7_above_sept6', 28 > 20, True),
        ('york_gap_metres', Decimal(2) * Decimal('0.3048') + Decimal(8) * Decimal('0.0254'), Decimal('0.8128')),
    ]
    for label, actual, expected in checks:
        require_equal(label, actual, expected)
    # Counterexample to mandatory opposite signs: two known points (10,0),
    # (10,10), receiver from (0,0) to (1,0). This is synthetic geometry.
    changes = (Decimal(81).sqrt() - Decimal(100).sqrt(),
               Decimal(181).sqrt() - Decimal(200).sqrt())
    if not changes[0] < 0 or not changes[1] < 0 or changes[0] == changes[1]:
        raise ValueError(f'Geometry counterexample failed: {changes}')
    return len(checks) + 1
