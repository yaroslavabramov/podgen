"""Temporary isolated measurement; external responses are retained local fixtures."""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path('/Users/yaroslavabramov/side/podgen')
PG = Path('/opt/homebrew/opt/postgresql@17/bin')


def worker(code, output, db_url):
    sys.path[:0] = [str(code/'backend/src'), str(code/'backend'), str(ROOT/'backend/benchmarks/offline_episode_replay')]
    from uuid import uuid4
    import httpx
    import httpx2
    from probe import Probe
    from podgen.domain.contracts.common import ArtifactRef, BlobRef, CodeProducer
    from podgen.persistence.database import Database, query
    from podgen.persistence.repository import Repository
    from podgen.runtime.evaluation.fact_source_experiment.measurements import MeasuredObjects
    from podgen.sources.acquisition import service
    from podgen.sources.acquisition.contracts import FetchedDocument
    from podgen.sources.extraction.contracts import ExtractedDocument
    from tests.source_acquisition_support import setup

    def forbidden(*args, **kwargs):
        raise AssertionError('Live HTTP is forbidden')
    httpx.Client.send = httpx.AsyncClient.send = forbidden
    httpx2.Client.send = httpx2.AsyncClient.send = forbidden
    service._check_ssrf = lambda _: None
    database = Database(db_url)
    objects = MeasuredObjects(output/'objects')
    repo = Repository(database, objects, uuid4())
    project, episode = uuid4(), uuid4()
    with database.transaction() as tx:
        query(tx, "INSERT INTO projects(project_id,namespace_kind) VALUES(:p,'tenant')", p=project)
        query(tx, "INSERT INTO project_members VALUES(:p,:a,'owner')", p=project, a=repo.actor_id)
        query(tx, "INSERT INTO episodes VALUES(:p,:e)", p=project, e=episode)
    producer = CodeProducer(kind='code',component='offline-document-audit',build_id='fixture/1',operation_id=uuid4())
    request, _ = setup(repo, (project, episode), producer, prefer_mediawiki=False)
    fixtures = ROOT/'tmp/hieroglyphs-fullpass-fixtures-2026-11-14'
    rows = [r for r in json.loads((fixtures/'fetch-fixtures.json').read_text()) if 'extracted' in r][:20]
    probe = Probe(database, output/'profile.json')
    summaries = []
    class Fetcher:
        calls = 0
        async def fetch(self, url, profile, headers=None):
            self.calls += 1
            assert url == self.row['document']['requested_url']
            return FetchedDocument(**self.row['document'], body=(fixtures/self.row['body_file']).read_bytes())
    class Extractor:
        calls = 0
        async def extract(self, document, profile, *, role):
            self.calls += 1
            return ExtractedDocument(**self.row['extracted'])
    fetcher, extractor = Fetcher(), Extractor()
    async def measure():
        first = None
        for i, row in enumerate(rows):
            command = request.model_copy(update=dict(operation_id=uuid4(), source_id=uuid4(), url=row['document']['requested_url']))
            fetcher.row = extractor.row = row
            before = objects.counts.copy()
            phase = f'document-{i+1:02d}'
            with probe.scope(phase):
                result = await service.acquire_source(repo, project, command, fetcher=fetcher, extractors=extractor)
            operation_objects = dict(objects.counts-before)
            assert len(result.sources) == 1 and len(result.sources[0].representation_refs) == 1
            with probe.scope('output-verification'):
                source = repo.get(project, result.sources[0].source_revision_ref)
                representation = repo.get(project, result.sources[0].representation_refs[0])
                blob = BlobRef.model_validate(representation.payload['canonical_text_blob'])
                assert repo.read_blob(project, representation.ref, blob).decode() == row['extracted']['text']
            summaries.append(dict(phase=phase, status=result.status, sql=probe.counts[phase], original_bytes=(fixtures/row['body_file']).stat().st_size, text_chars=len(row['extracted']['text']), title=source.payload['title'], objects=operation_objects))
            if first is None:
                first = command, result
        before_calls = fetcher.calls, extractor.calls
        with probe.scope('completed-resume'):
            assert await service.acquire_source(repo, project, first[0], fetcher=fetcher, extractors=extractor) == first[1]
        assert (fetcher.calls, extractor.calls) == before_calls
    try:
        asyncio.run(measure())
        probe.server.mark_completed_operation()
        with database.read_transaction() as tx:
            counts = dict(query(tx, 'SELECT kind,count(*) FROM artifact_revisions WHERE project_id=:p GROUP BY kind',p=project).all())
        result = dict(documents=summaries, fetch_calls=fetcher.calls, extraction_calls=extractor.calls, artifact_counts=counts, resume_sql=probe.counts['completed-resume'])
        (output/'summary.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(json.dumps(dict(documents=len(rows), document_sql=[v['sql'] for v in summaries], resume_sql=result['resume_sql'], artifacts=counts)))
    finally:
        probe.save()
        probe.server.close()
        database.engine.dispose()


def main():
    code, output = Path(sys.argv[1]), Path(sys.argv[2])
    if len(sys.argv)>3:
        return worker(code, output, sys.argv[3])
    output.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory(prefix='podgen-doc-audit-',dir='/tmp') as tmp:
        base=Path(tmp)
        socket=base/'socket'
        socket.mkdir()
        def run(*args, **kwargs):
            subprocess.run([str(a) for a in args], check=True, **kwargs)
        run(PG/'initdb','-D',base/'data','-U','runner','-A','trust','--no-locale','-E','UTF8',stdout=subprocess.DEVNULL)
        run(PG/'pg_ctl','-D',base/'data','-l',base/'postgres.log','-o',f"-k {socket} -p 55481 -h '' -c shared_preload_libraries=pg_stat_statements -c pg_stat_statements.track=all",'start',stdout=subprocess.DEVNULL)
        try:
            url=f'postgresql+psycopg://runner@/postgres?host={socket}&port=55481'
            env=dict(os.environ, PYTHONPATH=str(code/'backend/src'), PODGEN_DATABASE_URL=url)
            run(sys.executable,'-m','alembic','upgrade','head',cwd=code/'backend',env=env,stdout=subprocess.DEVNULL)
            run(PG/'psql','-h',socket,'-p','55481','-U','runner','-d','postgres','-v','ON_ERROR_STOP=1','-c','CREATE EXTENSION pg_stat_statements',stdout=subprocess.DEVNULL)
            run(sys.executable,Path(__file__),code,output,url,env=env)
        finally:
            run(PG/'pg_ctl','-D',base/'data','stop','-m','fast',stdout=subprocess.DEVNULL)

if __name__=='__main__':
    main()
