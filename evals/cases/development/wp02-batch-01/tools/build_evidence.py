"""Rebuild local import excerpts from frozen text; no canonical domain objects."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def span(directory, ident, text_name, locator, start, end):
    path = ROOT / 'sources' / directory / text_name
    text = path.read_text(encoding='utf-8')
    left = text.index(start)
    right = text.index(end, left) + len(end)
    quote = text[left:right]
    return dict(label=ident, representation=text_name, locator=locator,
                start_codepoint=left, end_codepoint=right, exact=quote,
                exact_sha256=hashlib.sha256(quote.encode()).hexdigest())


def build():
    gps = [
        span('gps', 'G1', 'nasa-introduction-2017.txt', 'PDF/slide 30', 'The position solution involves', 'four satellites'),
        span('gps', 'G2', 'nasa-introduction-2017.txt', 'PDF/slide 38; formula requires original visual inspection', 'Measurement Equation (cont)', 'solved for'),
        span('gps', 'G3', 'sps-2020.txt', '§2.2.2.1; printed 11 / PDF 21', 'Each satellite broadcasts LNAV', 'Satellite clock correction'),
        span('gps', 'G4', 'nasa-introduction-2017.txt', 'PDF/slide 35', 'To detect the GPS signal', 'maintain lock on code and \ncarrier signal parameters'),
        span('gps', 'G5', 'sps-2020.txt', 'C-4 / PDF 186', 'Pseudorange Measurement.', 'SPS signal ranging measurement.'),
        span('gps', 'G6', 'sps-2020.txt', 'C-4 / PDF 186', 'PVT Solution.', 'frequency offset relative to GPS time.'),
        span('gps', 'G7', 'sps-2020.txt', 'C-1 / PDF 183', 'Dilution of Precision (DOP).', 'TDOP for time.'),
        span('gps', 'G8', 'sps-2020.txt', 'C-8 / PDF 190', 'c   Speed of light', 'm/sec)'),
        span('gps', 'G9', 'sps-2020.txt', '§2.4.5; printed 32 / PDF 42', '2.4.5  Excluded Errors \n', 'Operator (user) error'),
        span('gps', 'G10', 'faa-how-it-works.txt', 'main: The basic GPS service paragraph', 'The time information is placed', 'latitude, longitude, altitude, and time.'),
    ]
    snow = [
        span('snow', 'S1', 'snow-1855.txt', 'printed 40; HTML #Page_40', 'I had an interview with the Board', 'removed on the following day.'),
        span('snow', 'S2', 'snow-1855.txt', 'printed 51–52; HTML #Page_51 / #Page_52', 'There is no doubt that the mortality', 'water had become free\nfrom it.'),
        span('snow', 'S3', 'snow-1855.txt', 'printed 38–39', 'There were a few cases of cholera in the neighbourhood\nof Broad Street', 'might possibly have been still more impure.'),
        span('snow', 'S4', 'snow-1855.txt', 'printed 39–40', 'I requested permission, therefore', 'removed on the following day.'),
        span('snow', 'S5', 'snow-1855.txt', 'printed 42', 'The Workhouse in Poland Street', 'for water.'),
        span('snow', 'S6', 'snow-1855.txt', 'printed 42', 'There is a Brewery in Broad Street', 'New River water.'),
        span('snow', 'S7', 'snow-1855.txt', 'printed 44–45', 'Dr. Fraser also first called my attention', 'in another part of this work.'),
        span('snow', 'S8', 'snow-1855.txt', 'printed 45–46', 'The deaths which occurred during this fatal outbreak', 'learn it by private inquiry.'),
        span('snow', 'S9', 'snow-1855.txt', 'printed 49–51; table headers and missing dates included', 'TABLE I.\nDate.\nNo. of Fatal Attacks.', 'persons who had lingered for several days in consecutive\nfever.'),
    ]
    # Extend G6 through the known-altitude exception: the first occurrence ends
    # after the unconstrained case, so resolve the explicit second sentence too.
    text = (ROOT / 'sources/gps/sps-2020.txt').read_text()
    start = gps[5]['start_codepoint']
    end = text.index('frequency offset relative to GPS time.', gps[5]['end_codepoint']) + len('frequency offset relative to GPS time.')
    gps[5].update(end_codepoint=end, exact=text[start:end],
                  exact_sha256=hashlib.sha256(text[start:end].encode()).hexdigest())
    for directory, items in [('gps', gps), ('snow', snow)]:
        (ROOT / 'sources' / directory / 'evidence.json').write_text(
            json.dumps(items, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    build()
