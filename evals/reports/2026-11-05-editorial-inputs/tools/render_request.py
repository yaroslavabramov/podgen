"""Render every visible request value without slicing; JSON-valued input text is expanded for reading."""
import argparse
import html
import json
from pathlib import Path


def render(value, key=''):
    if isinstance(value, dict):
        return ''.join('<section><h4>' + html.escape(k) + '</h4>' + render(v, k) + '</section>' for k, v in value.items())
    if isinstance(value, list):
        return '<ol start="0">' + ''.join('<li>' + render(v) + '</li>' for v in value) + '</ol>'
    if key == 'text' and isinstance(value, str):
        try:
            expanded = json.loads(value)
        except (ValueError, TypeError):
            pass
        else:
            if isinstance(expanded, (dict, list)):
                return '<p class="note">Содержимое JSON показано раскрытым; исходная строка целиком сохранена в JSON рядом.</p>' + render(expanded)
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return '<pre>' + html.escape(text) + '</pre>'


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('input', type=Path)
    p.add_argument('output', type=Path)
    p.add_argument('--title', required=True)
    p.add_argument('--note', default='')
    args = p.parse_args()
    data = json.loads(args.input.read_text())
    args.output.write_text('<!doctype html><html lang="ru"><meta charset="utf-8"><title>' + html.escape(args.title) + '</title><style>body{max-width:1040px;margin:40px auto;padding:0 24px;font:17px/1.55 system-ui;color:#17202a;background:#faf9f6}h1{font-size:30px}h4{margin-bottom:6px;color:#415366}section{border-left:2px solid #dce2e6;padding-left:16px;margin:18px 0}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit;margin:0}li{padding-left:8px;margin:12px 0}.note{color:#626b74;font-size:14px}</style><h1>' + html.escape(args.title) + '</h1><p>' + html.escape(args.note) + '</p>' + render(data) + '</html>')


if __name__ == '__main__':
    main()
