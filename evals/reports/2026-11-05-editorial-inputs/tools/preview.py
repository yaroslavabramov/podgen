"""Admit originals and exercise the ordinary offline CLI. Never invokes run/count/provider."""
import argparse
import json
from pathlib import Path
import subprocess
from uuid import uuid4

from preview_runtime import PreviewState, ROOT, cli_base, offline_environment

SOURCE = ROOT / 'evals/cases/development/wp02-batch-01'


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--case', required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--approved-plan', type=Path, help='Manually supplied story for a technical writer preview, or an actual accepted output')
    args = parser.parse_args()
    try:
        environment = offline_environment()
    except ValueError as error:
        parser.error(str(error))
    from podgen.persistence.database import Database, query
    from podgen.persistence.objects import FileObjectStore
    from podgen.persistence.repository import Repository

    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    project, actor, run = uuid4(), uuid4(), uuid4()
    repo = Repository(Database(environment['PODGEN_DATABASE_URL']), FileObjectStore(out / 'objects'), actor, purpose='evaluation')
    with repo.database.transaction() as tx:
        query(tx, "INSERT INTO projects(project_id,namespace_kind) VALUES(:p,'tenant')", p=project)
        query(tx, "INSERT INTO project_members VALUES(:p,:a,'owner')", p=project, a=actor)
    base = cli_base(str(project), str(actor), str(out / 'objects'))

    def cli(*values):
        result = subprocess.run([*base, *map(str, values)], cwd=ROOT, env=environment, text=True, capture_output=True)
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
    state = PreviewState(project=str(project), actor=str(actor), run_id=str(run), case=args.case,
                         objects=str(out / 'objects'), shared_ref_file=str(out / 'shared-ref.json'),
                         profile_ref_file=str(out / 'profile-ref.json'))
    save(out / 'state.json', state.as_payload(summary))
    print(json.dumps(dict(case=args.case, previews=summary, endpoint_called=False), ensure_ascii=False))
    repo.database.engine.dispose()


if __name__ == '__main__':
    main()
