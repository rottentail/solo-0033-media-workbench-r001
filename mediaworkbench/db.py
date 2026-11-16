"""Persistent state for the media processing service."""
import sqlite3
from contextlib import contextmanager

SCHEMA = '''
CREATE TABLE IF NOT EXISTS assets (id INTEGER PRIMARY KEY, path TEXT NOT NULL, fingerprint TEXT NOT NULL, metadata TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS projects (id INTEGER PRIMARY KEY, name TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS clips (id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id), asset_id INTEGER NOT NULL REFERENCES assets(id), start_ms INTEGER NOT NULL, end_ms INTEGER NOT NULL, position INTEGER NOT NULL, gain REAL NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS renders (id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id), revision INTEGER NOT NULL, status TEXT NOT NULL, snapshot TEXT NOT NULL, output_path TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS render_jobs (
    render_id INTEGER PRIMARY KEY REFERENCES renders(id),
    destination TEXT NOT NULL, cache_root TEXT NOT NULL,
    epoch INTEGER NOT NULL DEFAULT 0, owner TEXT,
    lease_until REAL NOT NULL DEFAULT 0,
    artifact_path TEXT, artifact_digest TEXT
);
CREATE TABLE IF NOT EXISTS render_segments (
    render_id INTEGER NOT NULL REFERENCES render_jobs(render_id),
    position INTEGER NOT NULL, cache_key TEXT NOT NULL,
    status TEXT NOT NULL, digest TEXT,
    PRIMARY KEY (render_id, position)
);
'''

def connect(path):
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    conn.execute('PRAGMA journal_mode=WAL')
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def write(conn):
    if conn.in_transaction:
        raise ValueError('write requires an idle connection')
    conn.execute('BEGIN IMMEDIATE')
    try:
        yield
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
