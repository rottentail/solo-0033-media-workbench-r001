"""Verified immutable media segments, serialized across worker processes."""
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from . import encoder
from .assets import fingerprint
from .tools import run


def digest(path, tick=None):
    result = hashlib.sha256()
    with open(path, 'rb') as source:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            result.update(chunk)
            if tick is not None:
                tick()
    return result.hexdigest()


def verified(path, expected, tick=None):
    try:
        return Path(path).is_file() and digest(path, tick) == expected
    except FileNotFoundError:
        return False


class SegmentCache:
    def __init__(self, root):
        self.root = Path(root).resolve()
        (self.root / 'segments').mkdir(parents=True, exist_ok=True)
        (self.root / 'locks').mkdir(exist_ok=True)

    def paths(self, key):
        if not isinstance(key, str) or re.fullmatch('[0-9a-f]{64}', key) is None:
            raise ValueError('invalid segment identity')
        return (self.root / 'segments' / (key + '.mp4'),
                self.root / 'segments' / (key + '.json'))

    def _receipt(self, key):
        media, manifest = self.paths(key)
        if not media.is_file() or not manifest.is_file():
            return None
        try:
            record = json.loads(manifest.read_text(encoding='utf-8'))
        except (OSError, UnicodeError, ValueError):
            return None
        if not isinstance(record, dict):
            return None
        if record.get('key') != key or not isinstance(record.get('digest'), str):
            return None
        if record.get('size') != media.stat().st_size:
            return None
        return record

    def _encode(self, clip, media, tick):
        encoder.segment(clip, media, tick)
        if fingerprint(clip['path'], tick) != clip['fingerprint']:
            raise ValueError('source changed during encoding')
        info = json.loads(run('ffprobe', ['-v', 'error', '-show_streams',
            '-show_format', '-of', 'json', str(media)], tick=tick))
        video = next((s for s in info['streams'] if s['codec_type'] == 'video'), {})
        audio = next((s for s in info['streams'] if s['codec_type'] == 'audio'), {})
        duration = (clip['end_ms'] - clip['start_ms']) / 1000
        if ((video.get('width'), video.get('height')) != (640, 360)
                or video.get('r_frame_rate') != '25/1'
                or audio.get('sample_rate') != '48000'
                or audio.get('channels') != 2
                or abs(float(info['format']['duration']) - duration) > .15):
            raise ValueError('encoded segment does not match the media profile')
        return {'key': clip['cache_key'], 'digest': digest(media, tick),
                'size': media.stat().st_size}

    def ensure(self, clip, tick):
        key = clip['cache_key']
        media, manifest = self.paths(key)
        tick()
        record = self._receipt(key)
        if record is not None:
            return media, record['digest'], True
        record = self._encode(clip, media, tick)
        receipt = json.dumps(record, sort_keys=True)
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                prefix='receipt-', suffix='.json', dir=self.root, delete=False) as handle:
            handle.write(receipt)
            handle.flush()
            os.fsync(handle.fileno())
            staged = Path(handle.name)
        try:
            tick()
            os.replace(staged, manifest)
        finally:
            staged.unlink(missing_ok=True)
        return media, record['digest'], False
