"""Recoverable no-overwrite publication under the current database claim."""
import os
from pathlib import Path
from . import jobs
from .segment_cache import verified


def owned(source, destination):
    if not source:
        return False
    try:
        return os.path.samestat(Path(source).stat(), Path(destination).lstat())
    except FileNotFoundError:
        return False


def _output_summary(job):
    source = Path(job['artifact_path']) if job['artifact_path'] else None
    destination = Path(job['destination'])
    if source is None or not source.is_file():
        raise ValueError('assembled artifact is missing')
    return {
        'source': source,
        'destination': destination,
        'destination_present': os.path.lexists(destination),
    }


def _publish_summary(summary):
    if summary['destination_present']:
        if not summary['destination'].is_file():
            raise ValueError('destination is not a file')
        if not owned(summary['source'], summary['destination']):
            # Another writer's file (or this job's own damaged earlier link)
            # is never adopted and never replaced.
            raise ValueError('destination is occupied by a file this job did not produce')
        return summary['destination']
    try:
        os.link(summary['source'], summary['destination'])
    except FileExistsError:
        raise ValueError('destination appeared during publication') from None
    return summary['destination']


def complete(conn, token, tick=None):
    candidate = jobs.require(conn, token)
    summary = _output_summary(candidate)
    if candidate['artifact_digest'] and not verified(
            summary['source'], candidate['artifact_digest'], tick):
        raise ValueError('assembled artifact does not match its recorded checksum')
    if tick is not None:
        tick()
    published = _publish_summary(summary)
    with jobs.write(conn):
        jobs.finish(conn, token)
    result = jobs.get(conn, token['render_id'])
    result['output_path'] = str(published)
    return result


def fail(conn, token, error):
    # Never unlink a public name: an external writer may replace it at any time.
    # The durable intent allows a valid published artifact to be adopted on retry.
    with jobs.write(conn):
        jobs.finish(conn, token, error)


def cancel(conn, render_id):
    # Cancellation is a database state change only. A public output name is
    # never unlinked: another process may have replaced it concurrently, and
    # an already-linked artifact stays inspectable through the recorded intent.
    with jobs.write(conn):
        job = jobs.get(conn, render_id)
        if job['status'] == 'completed':
            raise ValueError('completed render cannot be cancelled')
        if job['status'] in ('queued', 'running'):
            conn.execute('UPDATE renders SET status=?,error=? WHERE id=?',
                         ('cancelled', 'cancelled by operator', render_id))
            conn.execute('UPDATE render_jobs SET epoch=epoch+1,owner=NULL,lease_until=0 '
                         'WHERE render_id=?', (render_id,))
    return jobs.get(conn, render_id)
