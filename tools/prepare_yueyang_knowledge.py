"""Publish existing approved hospital documents; never approve or edit source material."""

import argparse
import json
import subprocess
from pathlib import Path

from meta_agent.domains.space_context import space_catalog

OUTPUT = Path(".local/space-scene")
RAG = "rag-portfolio-api-1"
WORKER = "rag-portfolio-worker-1"
REMOTE = r"""
import asyncio,json,sys
from uuid import UUID,uuid4
from sqlalchemy import select
from rag_portfolio.config import Settings
from rag_portfolio.db.session import Database
from rag_portfolio.db.models import (KnowledgeBase,DocumentVersion,SourceDocument,
    KnowledgeRelease,BackgroundJob,ReleaseDocument)
from rag_portfolio.services.documents import ActorContext
from rag_portfolio.services.releases import create_release,activate_release,verify_index
from rag_portfolio.services.jobs import JobQueue
from rag_portfolio.adapters.vector import QdrantConnection
from rag_portfolio.worker import run_once

args=json.load(sys.stdin)
async def main():
    settings=Settings(); database=Database(settings)
    try:
        async with database.sessions() as session:
            kb=await session.scalar(select(KnowledgeBase).where(
                KnowledgeBase.code=='hospital_kb',KnowledgeBase.hospital_id=='yueyang_hospital_demo',
                KnowledgeBase.tenant_id==settings.tenant_id))
            if kb is None: raise RuntimeError('existing_hospital_kb_missing')
            expected={'hospital-scene-guide','general-patient-guide','general-doctor-guide',
                      'yueyang-patient-guide','yueyang-doctor-guide'}
            rows=(await session.execute(select(SourceDocument,DocumentVersion).join(
                DocumentVersion,DocumentVersion.document_id==SourceDocument.id).where(
                SourceDocument.kb_id==kb.id,SourceDocument.external_key.in_(expected),
                DocumentVersion.review_status=='approved',DocumentVersion.revoked_at.is_(None)
            ).order_by(DocumentVersion.revision.desc()))).all()
            selected={}
            for source,version in rows: selected.setdefault(source.external_key,version)
            if set(selected)!=expected: raise RuntimeError('approved_documents_missing')
            guide=selected['hospital-scene-guide']
            if str(guide.id)!=args['catalog']['source_version_id']:
                raise RuntimeError('scene_guide_version_changed_review_catalog')
            if any(name not in guide.normalized_text for name in args['catalog']['scenes']):
                raise RuntimeError('scene_name_not_in_existing_rag_source')
            result={'kb_id':str(kb.id),'previous_enabled':kb.enabled,
                    'previous_release_id':str(kb.active_release_id)
                        if kb.active_release_id else None,
                    'source_version_id':str(guide.id),'source_hash':guide.content_hash,
                    'scene_count':len(args['catalog']['scenes']),
                    'selected_versions':{key:str(value.id) for key,value in selected.items()}}
            if args['action']=='inspect': return result
            if kb.active_release_id:
                active=await session.get(KnowledgeRelease,kb.active_release_id)
                ids=set(await session.scalars(select(ReleaseDocument.version_id).where(
                    ReleaseDocument.release_id==kb.active_release_id)))
                if (kb.enabled and active.status=='active'
                    and ids=={v.id for v in selected.values()}):
                    return {**result,'release_id':str(active.id),'status':'already_active'}
            pending=await session.scalar(select(BackgroundJob.id).where(
                BackgroundJob.tenant_id==settings.tenant_id,BackgroundJob.status.in_(['queued','running'])).limit(1))
            if pending: raise RuntimeError('finish_existing_rag_jobs_before_publishing')
            kb_id=kb.id;version_ids=[v.id for v in selected.values()]
        actor=ActorContext(settings.tenant_id,'space-workflow-integration',uuid4())
        async with database.sessions.begin() as session:
            release,job=await create_release(session,settings,actor,kb_id,version_ids)
            release_id=release.id;job_id=job.id
        queue=JobQueue(database.sessions,settings.tenant_id,settings.worker_lease_seconds,settings.worker_max_attempts)
        await run_once(queue,database=database,settings=settings)
        async with database.sessions() as session:
            job=await session.get(BackgroundJob,job_id)
            if job.status!='succeeded':
                raise RuntimeError('hospital_release_build_'+str(job.last_error_code))
        vector=QdrantConnection(settings)
        try:
            async with database.sessions.begin() as session:
                release=await session.get(KnowledgeRelease,release_id)
                await verify_index(vector.client,release)
                await activate_release(session,actor,release_id,
                    'Expose existing reviewed Yueyang documents to the space-scoped workflow')
        finally: await vector.close()
        return {**result,'release_id':str(release_id),'status':'active'}
    finally: await database.close()
print('SPACE_RELEASE_RESULT='+json.dumps(asyncio.run(main()),ensure_ascii=True))
"""


def remote(action):
    payload = json.dumps({"action": action, "catalog": next(iter(space_catalog().values()))})
    result = subprocess.run(
        ["docker", "exec", "-i", RAG, "python", "-c", REMOTE],
        input=payload,
        capture_output=True,
        encoding="utf-8",
        timeout=240,
    )
    if result.returncode:
        raise RuntimeError(result.stderr[-2000:])
    for line in result.stdout.splitlines():
        if line.startswith("SPACE_RELEASE_RESULT="):
            return json.loads(line.split("=", 1)[1])
    raise RuntimeError("RAG did not return a release result")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["inspect", "publish"])
    args = parser.parse_args()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    before = remote("inspect")
    backup = OUTPUT / "hospital-release-before.json"
    if not backup.exists():
        backup.write_text(json.dumps(before, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.action == "publish":
        # The existing worker predates release.build; use the API image's supported handler.
        running = (
            subprocess.check_output(
                ["docker", "inspect", "--format", "{{.State.Running}}", WORKER], text=True
            ).strip()
            == "true"
        )
        if running:
            subprocess.run(["docker", "stop", WORKER], check=True, capture_output=True)
        try:
            result = remote("publish")
        finally:
            if running:
                subprocess.run(["docker", "start", WORKER], check=True, capture_output=True)
        (OUTPUT / "hospital-release-after.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    else:
        result = before
    print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()
