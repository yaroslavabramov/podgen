"""Reuse validated data-only state with the fixed offline preview command."""
import argparse
import json
from pathlib import Path
import subprocess

from preview_runtime import PreviewState, ROOT, STAGES, offline_environment


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('state', type=Path)
    parser.add_argument('--stage', required=True, choices=STAGES)
    parser.add_argument('--out', required=True, type=Path)
    for name in ('candidate', 'plan-review', 'approved-plan', 'draft', 'feedback', 'fixed-ref'):
        parser.add_argument('--' + name, type=Path)
    args = parser.parse_args()
    try:
        state = PreviewState.from_payload(json.loads(args.state.read_text()))
        environment = offline_environment()
    except (OSError, ValueError) as error:
        parser.error(str(error))
    command = [*state.preview_command(), '--stage', args.stage, '--out', str(args.out.resolve())]
    for name in ('candidate', 'plan-review', 'approved-plan', 'draft', 'feedback', 'fixed-ref'):
        value = getattr(args, name.replace('-', '_'))
        if value is not None:
            if not value.is_file():
                parser.error('Input file does not exist: ' + str(value))
            command += ['--' + name, ('@' if name == 'fixed-ref' else '') + str(value.resolve())]
    return subprocess.run(command, cwd=ROOT, env=environment, check=False).returncode


if __name__ == '__main__':
    raise SystemExit(main())
