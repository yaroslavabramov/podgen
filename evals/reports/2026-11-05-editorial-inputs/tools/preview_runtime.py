"""Fixed offline CLI invocation and data-only state for report utilities."""
from dataclasses import asdict, dataclass
import os
from pathlib import Path
import sys
from uuid import UUID

ROOT = Path(__file__).resolve().parents[4]
STAGES = (
    'plan', 'plan-review', 'plan-revise', 'plan-review-revised',
    'draft', 'review', 'revise', 'anchor-repair', 'plan-target-repair',
)
CONTRACT = 'offline-editorial-preview/1'


def offline_environment():
    environment = {key: value for key, value in os.environ.items()
                   if not key.upper().startswith(('OPENAI_', 'AZURE_OPENAI_'))}
    if not environment.get('PODGEN_DATABASE_URL', '').strip():
        raise ValueError('Set PODGEN_DATABASE_URL explicitly; these utilities do not load .env.')
    environment['PYTHONPATH'] = str(ROOT / 'backend/src')
    return environment


def absolute_path(value, *, file=False):
    if not isinstance(value, str) or not value.strip() or '\x00' in value:
        raise ValueError('Expected an absolute local path.')
    path = Path(value)
    if not path.is_absolute():
        raise ValueError('Expected an absolute local path.')
    if file and not path.is_file():
        raise ValueError('Preview reference file does not exist.')
    return str(path.resolve())


def cli_base(project, actor, objects):
    return [sys.executable, '-m', 'podgen.writing_cli',
            '--project', str(UUID(project)), '--actor', str(UUID(actor)),
            '--objects', absolute_path(objects)]


@dataclass(frozen=True)
class PreviewState:
    project: str
    actor: str
    run_id: str
    case: str
    objects: str
    shared_ref_file: str
    profile_ref_file: str

    @classmethod
    def from_payload(cls, value):
        fields = set(cls.__dataclass_fields__)
        if not isinstance(value, dict) or set(value) != fields | {'contract', 'previews', 'endpoint_called'}:
            raise ValueError('Invalid preview state fields; regenerate state with preview.py.')
        if value['contract'] != CONTRACT or value['endpoint_called'] is not False or not isinstance(value['previews'], dict):
            raise ValueError('Invalid offline preview state.')
        for name in ('project', 'actor', 'run_id'):
            if not isinstance(value[name], str):
                raise ValueError('Expected a UUID string.')
            UUID(value[name])
        case = value['case']
        if not isinstance(case, str) or not case.strip() or any(ord(c) < 32 for c in case):
            raise ValueError('Expected a nonempty assignment key.')
        data = {name: value[name] for name in fields}
        for name in ('objects', 'shared_ref_file', 'profile_ref_file'):
            data[name] = absolute_path(data[name], file=name != 'objects')
        return cls(**data)

    def as_payload(self, previews):
        value = dict(contract=CONTRACT, **asdict(self), previews=previews, endpoint_called=False)
        self.from_payload(value)
        return value

    def preview_command(self):
        return [*cli_base(self.project, self.actor, self.objects), 'preview',
                '--shared-ref', '@' + self.shared_ref_file, '--case=' + self.case,
                '--profile', '@' + self.profile_ref_file, '--run-id', str(UUID(self.run_id))]
