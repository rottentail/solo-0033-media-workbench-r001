"""Resume persistent segment progress and assemble an export."""
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
import uuid
from . import encoder, jobs, planner, publication
from .segment_cache import SegmentCache, digest, verified


def _progress_segments(conn, token, job, snapshot, cache, tick):
    saved = {row['position']: row for row in job['segments']}
    ensured = {}
    segments = []
    for position, clip in enumerate(snapshot['clips']):
        tick()
        key = clip['cache_key']
        checkpoint = saved.get(position)
        if (checkpoint is not None and checkpoint['status'] == 'ready'
                and checkpoint['cache_key'] == key and checkpoint['digest']):
            path, _ = cache.paths(key)
            if verified(path, checkpoint['digest'], tick):
                ensured.setdefault(key, (path, checkpoint['digest']))
                segments.append(path)
                continue
            # Damaged or mismatched progress is rebuilt below.
        if key in ensured:
            path, checksum = ensured[key]
        else:
            path, checksum, _ = cache.ensure(clip, tick)
            ensured[key] = (path, checksum)
        jobs.checkpoint(conn, token, position, key, checksum)
        # Every occurrence keeps its own position in the concat list, even
        # when its encoded bytes are shared with another occurrence.
        segments.append(path)
    return segments


def _assembly_directory(job, token):
    parent = Path(job['destination']).parent
    prefix = f'.media-render-{job["id"]}-{token["epoch"]}-'
    return Path(tempfile.mkdtemp(prefix=prefix, dir=parent))


def run(conn, render_id, owner=None, lease_seconds=30):
    token = jobs.claim(conn, render_id, owner or uuid.uuid4().hex, lease_seconds)
    next_renewal = 0

    def tick():
        nonlocal next_renewal
        now = time.monotonic()
        if now >= next_renewal:
            jobs.renew(conn, token, lease_seconds)
            next_renewal = now + min(1, lease_seconds / 4)
        else:
            jobs.require(conn, token)

    try:
        job = jobs.get(conn, render_id)
        snapshot = json.loads(job['snapshot'])
        planner.verify_sources(snapshot, tick)
        cache = SegmentCache(job['cache_root'])
        if (job['artifact_path'] and job['artifact_digest']
                and verified(Path(job['artifact_path']), job['artifact_digest'], tick)):
            # Assembly finished before the interruption; publish the verified
            # artifact instead of re-encoding a sound result.
            return publication.complete(conn, token, tick)
        segments = _progress_segments(conn, token, job, snapshot, cache, tick)
        planner.verify_sources(snapshot, tick)
        directory = _assembly_directory(job, token)
        retained = False
        try:
            output = encoder.join(segments, directory, tick)
            checksum = digest(output, tick)
            with open(output, 'rb') as handle:
                os.fsync(handle.fileno())
            jobs.artifact(conn, token, output, checksum)
            retained = True
            return publication.complete(conn, token, tick)
        finally:
            if not retained:
                shutil.rmtree(directory)
    except jobs.LeaseLost:
        raise
    except Exception as exc:
        try:
            publication.fail(conn, token, exc)
        except jobs.LeaseLost:
            pass
        raise
