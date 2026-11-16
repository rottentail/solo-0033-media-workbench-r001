"""Run bounded media workflows on generated inputs without external services."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import tempfile
import time
from . import db, assets, timeline, renders, jobs, worker
from .tools import run


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--workflow', choices=['all', 'timeline', 'render', 'recovery'], default='all')
    workflow = parser.parse_args().workflow
    with tempfile.TemporaryDirectory(prefix='media-check-') as directory:
        root = Path(directory)
        source = root / 'source.mp4'
        run('ffmpeg', ['-v','error','-y','-f','lavfi','-i','testsrc=size=160x90:rate=25',
            '-f','lavfi','-i','sine=frequency=440:sample_rate=48000','-t','2',
            '-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac',str(source)])
        database = root / 'data.sqlite3'
        with closing(db.connect(database)) as conn:
            asset = assets.register(conn, source)
            project = timeline.project(conn, 'Example timeline')
            timeline.add_clip(conn, project['id'], asset['id'], 0, 1000, .5)
            timeline.add_clip(conn, project['id'], asset['id'], 1000, 2000)
            if workflow in ('all', 'timeline'):
                edited = timeline.replace(conn, project['id'], [
                    {'asset_id': asset['id'], 'start_ms': 1000, 'end_ms': 2000, 'gain': .5},
                    {'asset_id': asset['id'], 'start_ms': 0, 'end_ms': 1000},
                ], expected_revision=2)
                assert edited['project']['revision'] == 3
                try:
                    timeline.replace(conn, project['id'], [], expected_revision=2)
                except ValueError:
                    pass
                else:
                    raise AssertionError('stale edit accepted')
                assert len(timeline.snapshot(conn, project['id'])['clips']) == 2
                print('Timeline workflow passed: atomic reorder and stale revision rejection.')
            if workflow in ('all', 'render'):
                result = renders.create(conn, project['id'], root / 'output.mp4')
                probe = json.loads(run('ffprobe', ['-v','error','-show_streams','-show_format',
                                                  '-of','json',result['output_path']]))
                assert 1.9 < float(probe['format']['duration']) < 2.2
                assert {stream['codec_type'] for stream in probe['streams']} >= {'video','audio'}
                assert renders.get(conn, result['id'])['status'] == 'completed'
                print('Media workflow passed: two clips, audio and video, 2 seconds.')
            if workflow in ('all', 'recovery'):
                queued = renders.queue(conn, project['id'], root / 'recovered.mp4')
                jobs.claim(conn, queued['id'], 'departed-worker', seconds=.2)
        if workflow in ('all', 'recovery'):
            time.sleep(.25)
            with closing(db.connect(database)) as resumed:
                result = worker.run(resumed, queued['id'])
                assert result['status'] == 'completed' and result['epoch'] == 2
                assert all(segment['status'] == 'ready' for segment in result['segments'])
                cancelled = renders.queue(resumed, project['id'], root / 'cancelled.mp4')
                assert renders.cancel(resumed, cancelled['id'])['status'] == 'cancelled'
                assert not (root / 'cancelled.mp4').exists()
                print('Recovery workflow passed: persisted plan, expired claim takeover and cancellation.')


if __name__ == '__main__':
    main()
