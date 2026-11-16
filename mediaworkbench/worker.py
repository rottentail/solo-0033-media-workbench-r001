"""Resume persistent segment progress and assemble an export."""
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
import uuid
from . import encoder, jobs, planner, publication
from .segment_cache import SegmentCache, digest


def _progress_segments(conn, token, job, snapshot, cache, tick):
    saved = {row['position']: row for row in job['segments']}
    reusable = {}
    segments = []
    for position, clip in enumerate(snapshot['clips']):
        tick()
        key = clip['cache_key']
        checkpoint = saved.get(position)
        if checkpoint is not None and checkpoint['status'] == 'ready':
            path, _ = cache.paths(checkpoint['cache_key'])
            if path.is_file():
                if key not in reusable:
                    reusable[key] = path
                    segments.append(path)
                continue
        path, checksum, _ = cache.ensure(clip, tick)
        jobs.checkpoint(conn, token, position, key, checksum)
        reusable[key] = path
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
