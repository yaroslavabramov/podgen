"""Optional frozen-source extraction; requires pypdf==6.21.4, beautifulsoup4==4.15.0.

Run in a COPY of the batch, then compare .txt and rebuild evidence. No network.
The page-38 render is an inspected aid; PDF remains the formula's original.
"""
from pathlib import Path
from bs4 import BeautifulSoup
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]


def normalize(text):
    return text.replace('\r\n', '\n').replace('\r', '\n')


def main():
    for directory, name in [('gps', 'sps-2020'), ('gps', 'nasa-introduction-2017'),
                            ('snow', 'parish-1855-b21363870')]:
        path = ROOT / 'sources' / directory / f'{name}.pdf'
        pages = PdfReader(path).pages
        text = '\n\n'.join(f'=== PDF PAGE {i+1} ===\n' + page.extract_text()
                           for i, page in enumerate(pages))
        path.with_suffix('.txt').write_text(normalize(text), encoding='utf-8')
    for directory, name, main_only in [('snow', 'snow-1855', False),
                                        ('gps', 'faa-how-it-works', True)]:
        path = ROOT / 'sources' / directory / f'{name}.html'
        soup = BeautifulSoup(path.read_bytes(), 'html.parser')
        content = soup.find('main') if main_only else soup
        if content is None:
            raise ValueError(f'Missing main content: {path}')
        path.with_suffix('.txt').write_text(normalize(content.get_text('\n', strip=True)), encoding='utf-8')


if __name__ == '__main__':
    main()
