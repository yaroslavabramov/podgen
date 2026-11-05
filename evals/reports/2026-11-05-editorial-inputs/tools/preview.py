"""Admit originals and exercise the ordinary offline CLI. Never invokes run/count/provider."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[4]
SOURCE = ROOT / 'evals/cases/development/wp02-batch-01'


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--approved-plan', type=Path, help='Conditional own-plan fixture or actual accepted output')
    args = parser.parse_args()
    load_dotenv(ROOT / '.env')
    if not os.environ.get('PODGEN_DATABASE_URL'):
        parser.error('Set PODGEN_DATABASE_URL to the migrated local evaluation database.')
    from podgen.persistence.database import Database, query
    from podgen.persistence.objects import FileObjectStore
    from podgen.persistence.repository import Repository

    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    project, actor, run = uuid4(), uuid4(), uuid4()
    repo = Repository(Database(os.environ['PODGEN_DATABASE_URL']), FileObjectStore(out / 'objects'), actor, purpose='evaluation')
    with repo.database.transaction() as tx:
        query(tx, "INSERT INTO projects(project_id,namespace_kind) VALUES(:p,'tenant')", p=project)
        query(tx, "INSERT INTO project_members VALUES(:p,:a,'owner')", p=project, a=actor)
    base = [sys.executable, '-m', 'podgen.writing_cli', '--project', str(project), '--actor', str(actor), '--objects', str(out / 'objects')]

    def cli(*values):
        result = subprocess.run([*base, *map(str, values)], cwd=ROOT, text=True, capture_output=True)
        if result.returncode:
            raise RuntimeError(result.stderr or result.stdout)
        return json.loads(result.stdout)

    packet_path = ROOT / 'evals/editorial-inputs/v1' / args.case / 'shared.json'
    packet = json.loads(packet_path.read_text())
    mapping = {}
    for index, document in enumerate(packet['documents']):
        ref = out / f'source-{index}.json'
        cli('editorial-source-import', '--file', SOURCE / document['source_path'], '--source-path', document['source_path'], '--scope', args.case, '--purpose', document['purpose'], '--out', ref)
        mapping[document['source_path']] = json.loads(ref.read_text())
    save(out / 'source-map.json', mapping)
    cli('editorial-import', '--input', packet_path, '--source-map', out / 'source-map.json', '--out', out / 'shared-ref.json')
    cli('profile-create', '--model', 'gpt-5.4-2026-03-05', '--context-limit', 1050000, '--output-limit', 32768, '--reasoning-effort', 'medium', '--roles', 'writer', 'editorial_reviewer', 'planner', 'plan_reviewer', '--out', out / 'profile-ref.json')
    preview = ['preview', '--shared-ref', '@' + str(out / 'shared-ref.json'), '--case', args.case, '--profile', '@' + str(out / 'profile-ref.json'), '--run-id', str(run)]
    stages = ['plan'] + (['draft'] if args.approved_plan else [])
    summary = {}
    for stage in stages:
        extra = ['--approved-plan', args.approved_plan.resolve()] if stage == 'draft' else []
        cli(*preview, '--stage', stage, *extra, '--out', out / f'{stage}.preview.json')
        result = json.loads((out / f'{stage}.preview.json').read_text())
        assert result['endpoint_called'] is False
        body = result['request']
        save(out / f'{stage}.visible.json', dict(instructions=body['instructions'], input=body['input'], schema=body['text']['format']['schema']))
        summary[stage] = result['visible_bytes']
    save(out / 'state.json', dict(project=str(project), actor=str(actor), run_id=str(run), cli=base, preview=preview, previews=summary, endpoint_called=False))
    print(json.dumps(dict(case=args.case, previews=summary, endpoint_called=False), ensure_ascii=False))
    repo.database.engine.dispose()


if __name__ == '__main__':
    main()
