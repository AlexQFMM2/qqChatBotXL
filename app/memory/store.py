"""Small synchronous SQLite transactions. No network/model work under these locks.

The append-only fsynced journal and its witness are deliberately OUTSIDE backups.
Every read replays committed revocations before returning any facts.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .identity import Actor, digest

TZ = ZoneInfo('Asia/Shanghai')


class MemoryUnavailable(RuntimeError):
    pass


class MemoryDenied(ValueError):
    pass


def packed(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def atomic(path, data):
    temporary = path.with_name('.' + path.name + '.tmp')
    fd = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_DIRECTORY | os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def keywords(text):
    # Chinese bigrams work without a dictionary/model dependency.
    result = set(re.findall(r'[a-z0-9_]{2,}', text.lower()))
    for word in re.findall(r'[\u4e00-\u9fff]+', text):
        result.update(word[i:i+2] for i in range(len(word)-1))
    return result - {'什么', '怎么', '记得', '记忆', '一下', '我的', '我是', '现在', '这个'}


class SharedMemory:
    def __init__(self, directory, *, clock=time.time):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.clock = clock
        self.path = self.directory / 'memory.db'
        self.log = self.directory / 'revocations.jsonl'
        self.head = self.directory / 'journal-head.json'
        self._runtime = open(self.directory / 'runtime.lock', 'a+b')
        try:
            fcntl.flock(self._runtime, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            self._runtime.close()
            raise MemoryUnavailable('共享记忆正在离线恢复') from None
        self.db = sqlite3.connect(self.path, timeout=.25, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('PRAGMA foreign_keys=ON')
        try:
            with self.lock():
                version = self.db.execute('PRAGMA user_version').fetchone()[0]
                if version not in (0, 1):
                    raise MemoryUnavailable('共享记忆格式版本不兼容')
                self.db.executescript('''
                    CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS facts(
                        id TEXT PRIMARY KEY, subject TEXT NOT NULL, content TEXT NOT NULL,
                        evidence TEXT NOT NULL, event TEXT NOT NULL, origin TEXT NOT NULL,
                        acl TEXT NOT NULL, revision INTEGER NOT NULL, deleted INTEGER NOT NULL,
                        created REAL NOT NULL, updated REAL NOT NULL, topic TEXT NOT NULL,
                        conflict INTEGER NOT NULL DEFAULT 0, compacted INTEGER NOT NULL DEFAULT 0);
                    CREATE TABLE IF NOT EXISTS grants(
                        fact TEXT NOT NULL, scope TEXT NOT NULL, PRIMARY KEY(fact,scope));
                    CREATE INDEX IF NOT EXISTS grants_scope ON grants(scope,fact);
                    CREATE TABLE IF NOT EXISTS versions(
                        id TEXT NOT NULL, revision INTEGER NOT NULL, snapshot TEXT NOT NULL,
                        retired REAL NOT NULL, PRIMARY KEY(id,revision));
                    CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, created REAL NOT NULL);
                    CREATE TABLE IF NOT EXISTS barriers(subject TEXT PRIMARY KEY, before_ns INTEGER NOT NULL);
                    CREATE TABLE IF NOT EXISTS suppressed(event TEXT PRIMARY KEY);
                    CREATE TABLE IF NOT EXISTS budgets(day TEXT NOT NULL, kind TEXT NOT NULL,
                        calls INTEGER NOT NULL, PRIMARY KEY(day,kind));
                    CREATE TABLE IF NOT EXISTS summaries(id TEXT PRIMARY KEY, subject TEXT NOT NULL,
                        acl TEXT NOT NULL, content TEXT NOT NULL, refs TEXT NOT NULL, created REAL NOT NULL);
                ''')
                identity = self.meta('identity')
                if not identity:
                    if self.log.exists() or self.head.exists():
                        raise MemoryUnavailable('记忆库缺失或初始化中断；请离线恢复，不能重建空库')
                    identity = uuid.uuid4().hex
                    self.db.execute("INSERT INTO meta VALUES ('identity',?)", (identity,))
                    self._append({'kind': 'init', 'identity': identity}, [])
                self._sync()
                self.db.execute('PRAGMA user_version=1')
        except Exception:
            self.close()
            raise

    def close(self):
        self.db.close()
        self._runtime.close()

    @contextmanager
    def lock(self, name='write.lock'):
        with open(self.directory / name, 'a+b') as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise MemoryUnavailable('记忆正在维护，请稍后重试') from None
            yield

    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
        except BaseException:
            self.db.rollback()
            raise
        else:
            self.db.commit()

    def meta(self, key, default=''):
        row = self.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return row[0] if row else default

    def setmeta(self, key, value):
        self.db.execute('INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                        (key, str(value)))

    def _journal(self):
        if not self.log.is_file() or not self.head.is_file():
            raise MemoryUnavailable('撤销日志或完整性见证缺失；共享记忆已停用')
        try:
            records = [json.loads(line) for line in self.log.read_text().splitlines()]
            head = json.loads(self.head.read_text())
            previous = ''
            for i, record in enumerate(records, 1):
                body = {'seq': i, 'previous': previous, 'data': record['data']}
                value = hashlib.sha256(packed(body).encode()).hexdigest()
                if record != {**body, 'hash': value}:
                    raise ValueError('chain')
                previous = value
            witnesses = head.get('rotating', [head])
            matching = [item for item in witnesses if 1 <= item['seq'] <= len(records)
                        and records[item['seq']-1]['hash'] == item['hash']]
            if (not records or records[0]['data'] != {'kind': 'init', 'identity': self.meta('identity')}
                    or not matching or int(self.meta('journal_seq', '0')) > len(records)):
                raise ValueError('head')
            # A crash after journal fsync but before witness fsync is recoverable.
            if 'rotating' in head or matching[0]['seq'] < len(records):
                atomic(self.head, packed({'seq': len(records), 'hash': previous}).encode())
            return records
        except (ValueError, KeyError, TypeError, IndexError) as exc:
            raise MemoryUnavailable('撤销日志不完整；共享记忆已停用') from exc

    def _append(self, data, records):
        body = {'seq': len(records)+1, 'previous': records[-1]['hash'] if records else '', 'data': data}
        record = {**body, 'hash': hashlib.sha256(packed(body).encode()).hexdigest()}
        fd = os.open(self.log, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, 'ab') as stream:
            stream.write((packed(record)+'\n').encode())
            stream.flush()
            os.fsync(stream.fileno())
        atomic(self.head, packed({'seq': record['seq'], 'hash': record['hash']}).encode())
        return record

    def _write_fact(self, fact):
        columns = tuple(fact)
        self.db.execute('INSERT OR REPLACE INTO facts ('+','.join(columns)+') VALUES ('+
                        ','.join('?' for _ in columns)+')', tuple(fact.values()))
        self.db.execute('DELETE FROM grants WHERE fact=?', (fact['id'],))
        if not fact['deleted']:
            self.db.executemany('INSERT INTO grants VALUES (?,?)',
                                [(fact['id'], scope) for scope in json.loads(fact['acl'])])

    def _sync(self):
        records = self._journal()
        last = int(self.meta('journal_seq', '0'))
        if last == len(records):
            return records
        with self.transaction():
            for record in records[last:]:
                data = record['data']
                if data['kind'] == 'revise':
                    fact = data['fact']
                    old = self.db.execute('SELECT * FROM facts WHERE id=?', (fact['id'],)).fetchone()
                    if old:
                        self.db.execute('INSERT OR IGNORE INTO versions VALUES (?,?,?,?)',
                            (old['id'], old['revision'], packed(dict(old)), data['at']))
                    self._write_fact(fact)
                    self.db.execute('INSERT INTO barriers VALUES (?,?) ON CONFLICT(subject) DO UPDATE '
                        'SET before_ns=max(before_ns,excluded.before_ns)', (fact['subject'], data['before_ns']))
                    self.db.execute('INSERT OR IGNORE INTO suppressed VALUES (?)', (fact['event'],))
                    if data.get('operation_event'):
                        self.db.execute('INSERT OR IGNORE INTO suppressed VALUES (?)', (data['operation_event'],))
                    self.db.execute('DELETE FROM summaries WHERE subject=?', (fact['subject'],))
                elif data['kind'] == 'retired':
                    self.db.execute('INSERT INTO barriers VALUES (?,?) ON CONFLICT(subject) DO UPDATE '
                        'SET before_ns=max(before_ns,excluded.before_ns)', (data['subject'], data['before_ns']))
                    self.db.execute('INSERT OR IGNORE INTO suppressed VALUES (?)', (data['event'],))
                    if data.get('operation_event'):
                        self.db.execute('INSERT OR IGNORE INTO suppressed VALUES (?)', (data['operation_event'],))
                self.setmeta('journal_seq', record['seq'])
        return records

    def reserve(self, kind, maximum):
        day = datetime.fromtimestamp(self.clock(), TZ).date().isoformat()
        with self.lock():
            self._sync()
            with self.transaction():
                self.db.execute('INSERT OR IGNORE INTO budgets VALUES (?,?,0)', (day, kind))
                return bool(self.db.execute('UPDATE budgets SET calls=calls+1 WHERE day=? AND kind=? '
                    'AND calls<?', (day, kind, maximum)).rowcount)

    def ingest(self, event, candidates):
        with self.lock():
            self._sync()
            with self.transaction():
                if self.db.execute('SELECT 1 FROM events WHERE id=?', (event['id'],)).fetchone():
                    return 0
                self.db.execute('INSERT INTO events VALUES (?,?)', (event['id'], self.clock()))
                barrier = self.db.execute('SELECT before_ns FROM barriers WHERE subject=?',
                                          (event['subject'],)).fetchone()
                if barrier and event['ns'] <= barrier[0]:
                    return 0  # An old pending extraction must not undo a correction/revocation.
                count = 0
                for candidate in candidates[:8]:
                    content = str(candidate.get('content', '')).strip()
                    evidence = str(candidate.get('evidence', '')).strip()
                    topic = str(candidate.get('topic', '')).strip()[:80]
                    if (not content or len(content) > 500 or not evidence or len(evidence) > 500
                            or evidence not in event['text'] or not topic):
                        continue
                    acl = [event['scope']]
                    # Only explicit first-person statements may leave an owner's QQ group.
                    if event['owner'] and event['channel'] == 'qq' and candidate.get('self') is True \
                            and re.search(r'我(?!们)|\b(?:I|my)\b', evidence, re.I):
                        acl.append('private:' + event['subject'])
                    acl = packed(sorted(acl))
                    identity = digest(event['id'], content)[:24]
                    existing = self.db.execute('SELECT id,content FROM facts WHERE subject=? AND '
                        'acl=? AND topic=? AND deleted=0', (event['subject'], acl, topic)).fetchall()
                    conflict = any(row['content'] != content for row in existing)
                    if conflict:
                        self.db.executemany('UPDATE facts SET conflict=1,revision=revision+1,compacted=0 WHERE id=?',
                                            [(row['id'],) for row in existing])
                        self.db.execute('DELETE FROM summaries WHERE subject=? AND acl=?',
                                        (event['subject'], acl))
                    fact = dict(id=identity, subject=event['subject'], content=content, evidence=evidence,
                        event=event['id'], origin=event['scope'], acl=acl, revision=1, deleted=0,
                        created=event['ns']/1e9, updated=self.clock(), topic=topic,
                        conflict=int(conflict), compacted=0)
                    self._write_fact(fact)
                    count += 1
                return count

    def needs_extraction(self, event):
        with self.lock():
            self._sync()
            if self.db.execute('SELECT 1 FROM events WHERE id=?', (event['id'],)).fetchone():
                return False
            barrier = self.db.execute('SELECT before_ns FROM barriers WHERE subject=?',
                                      (event['subject'],)).fetchone()
            return not barrier or event['ns'] > barrier[0]

    def visible(self, actor, query='', *, limit=8, chars=3000):
        with self.lock():
            self._sync()
            # Permission filtering happens in SQL before ranking or serializing content.
            rows = self.db.execute('SELECT f.* FROM facts f JOIN grants g ON g.fact=f.id '
                'WHERE g.scope=? AND f.deleted=0 ORDER BY f.updated DESC', (actor.scope,)).fetchall()
            terms = keywords(query)
            scored = []
            for row in rows:
                overlap = len(terms & keywords(row['content']+' '+row['topic']))
                if terms and not overlap:
                    continue
                scored.append((overlap, row['subject'] == actor.person, row['updated'], dict(row)))
            scored.sort(key=lambda item: item[:3], reverse=True)
            result, used = [], 0
            for *_, row in scored:
                size = len(self.render(row))
                if used + size <= chars:
                    result.append(row)
                    used += size
                if len(result) >= limit:
                    break
            return result

    @staticmethod
    def render(row):
        stamp = datetime.fromtimestamp(row['created'], TZ).strftime('%Y-%m-%d')
        return f"[{row['id']} v{row['revision']} {stamp}{' 有冲突，需确认' if row['conflict'] else ''}] {row['content']}"

    def recall(self, actor, query):
        rows = self.visible(actor, query)
        refs = {row['id']: row['revision'] for row in rows}
        with self.lock():
            self._sync()
            summaries = self.db.execute('SELECT * FROM summaries WHERE acl IN '
                '(SELECT DISTINCT f.acl FROM facts f JOIN grants g ON f.id=g.fact WHERE g.scope=?) '
                'ORDER BY created DESC', (actor.scope,)).fetchall()
            covered, lines = set(), []
            for summary in summaries:
                sources = json.loads(summary['refs'])
                if len(sources) < 2 or covered.intersection(sources):
                    continue
                if all(refs.get(identity) == revision for identity, revision in sources.items()):
                    rendered = '[整理摘要，依据 '+','.join(sources)+'] '+summary['content']
                    if len(rendered) < sum(len(self.render(row)) for row in rows if row['id'] in sources):
                        lines.append(rendered)
                        covered.update(sources)
            lines.extend(self.render(row) for row in rows if row['id'] not in covered)
            return '\n'.join(lines)[:3000], refs

    def validate(self, actor, refs):
        with self.lock():
            self._sync()
            for identity, revision in refs.items():
                row = self.db.execute('SELECT f.revision FROM facts f JOIN grants g ON g.fact=f.id '
                    'WHERE f.id=? AND f.deleted=0 AND g.scope=?', (identity, actor.scope)).fetchone()
                if not row or row[0] != revision:
                    return False
            return True

    def source_valid(self, event):
        with self.lock():
            self._sync()
            return not self.db.execute('SELECT 1 FROM suppressed WHERE event=?', (event,)).fetchone()

    def revise(self, actor, identity, operation, value='', *, event=''):
        with self.lock():
            records = self._sync()
            row = self.db.execute('SELECT f.* FROM facts f JOIN grants g ON g.fact=f.id '
                'WHERE f.id=? AND g.scope=? AND f.deleted=0', (identity, actor.scope)).fetchone()
            if not row:
                raise MemoryDenied('当前场景没有这条可操作记忆')
            fact = dict(row)
            private_origin = fact['origin'].startswith('private:')
            group_admin = actor.role in {'owner', 'admin'} and fact['origin'] == actor.scope
            if private_origin and not (actor.private and fact['subject'] == actor.person):
                raise MemoryDenied('私有记忆请在本人微信中管理')
            if fact['subject'] != actor.person and not group_admin:
                raise MemoryDenied('只能操作自己的记忆或管理当前来源群的记录')
            if operation in {'share', 'unshare'} and not (actor.private and fact['subject'] == actor.person):
                raise MemoryDenied('共享授权和撤销请在本人微信中操作')
            if operation == 'edit':
                if not value.strip() or len(value) > 500:
                    raise MemoryDenied('纠正内容须为 1～500 字')
                fact.update(content=value.strip(), evidence=value.strip(), conflict=0)
            elif operation == 'forget':
                fact['deleted'] = 1
                fact.update(content='', evidence='', topic='')
            elif operation in {'share', 'unshare'}:
                if not value.startswith('group:'):
                    raise MemoryDenied('只能显式授权指定 QQ 群')
                acl = set(json.loads(fact['acl']))
                if operation == 'share':
                    acl.add(value)
                else:
                    acl.discard(value)
                fact['acl'] = packed(sorted(acl))
            else:
                raise MemoryDenied('未知记忆操作')
            fact.update(revision=fact['revision']+1, updated=self.clock(), compacted=0)
            # Journal durability precedes visible mutation. Recovery replays this exact version.
            self._append({'kind': 'revise', 'fact': fact, 'at': self.clock(),
                          'operation_event': event, 'actor': actor.person, 'scope': actor.scope,
                          'before_ns': int(self.clock()*1e9)}, records)
            self._sync()
            return fact

    def batches(self):
        with self.lock():
            self._sync()
            rows = self.db.execute('SELECT * FROM facts WHERE deleted=0 AND compacted<revision '
                                   'ORDER BY subject,acl,updated').fetchall()
            batches, batch, size, partition = [], [], 0, None
            for row in rows:
                key = (row['subject'], row['acl'])
                length = len(packed({key: row[key] for key in ('id','content','evidence','topic','conflict')}))+1
                if batch and (key != partition or len(batch) >= 50 or size+length > 11998):
                    batches.append(batch)
                    batch, size = [], 0
                partition = key
                batch.append(dict(row))
                size += length
            if batch:
                batches.append(batch)
            return batches

    def publish(self, batch, content):
        with self.lock():
            self._sync()
            with self.transaction():
                for old in batch:
                    current = self.db.execute('SELECT * FROM facts WHERE id=?', (old['id'],)).fetchone()
                    if (not current or current['revision'] != old['revision'] or current['deleted']
                            or current['acl'] != old['acl'] or current['conflict'] != old['conflict']):
                        return False
                refs = {row['id']: row['revision'] for row in batch}
                self.db.execute('INSERT INTO summaries VALUES (?,?,?,?,?,?)',
                    (uuid.uuid4().hex, batch[0]['subject'], batch[0]['acl'], content[:3000],
                     packed(refs), self.clock()))
                self.db.executemany('UPDATE facts SET compacted=revision WHERE id=?',
                                   [(row['id'],) for row in batch])
                return True

    def backup(self):
        with self.lock():
            self._sync()
            directory = self.directory / 'backups'
            directory.mkdir(exist_ok=True, mode=0o700)
            day = datetime.fromtimestamp(self.clock(), TZ).date().isoformat()
            target = directory / (day+'.db')
            temporary = directory / (day+'.tmp')
            with sqlite3.connect(temporary) as destination:
                self.db.backup(destination)
            os.chmod(temporary, 0o600)
            os.replace(temporary, target)
            cutoff = datetime.fromtimestamp(self.clock(), TZ).date()-timedelta(days=13)
            for path in directory.glob('????-??-??.db'):
                if path.stem < cutoff.isoformat():
                    path.unlink()
            with self.transaction():
                self.db.execute('DELETE FROM versions WHERE retired<?', (self.clock()-30*86400,))
            self._retire_journal()
            return target

    def _retire_journal(self):
        """Keep irreversible barriers forever, but erase superseded fact text after 30 days.

        Preserve sequence numbers for old backups. A two-head witness makes the
        atomic log replacement recoverable at every crash boundary.
        """
        records = self._journal()
        latest, next_at = {}, {}
        changed = False
        data_rows = [record['data'] for record in records]
        for i in range(len(records)-1, -1, -1):
            data = data_rows[i]
            if data['kind'] != 'revise':
                continue
            identity = data['fact']['id']
            if identity in latest and next_at[identity] < self.clock()-30*86400:
                data_rows[i] = {'kind':'retired', 'id':identity, 'subject':data['fact']['subject'],
                                'event':data['fact']['event'], 'before_ns':data['before_ns'],
                                'operation_event':data.get('operation_event', '')}
                changed = True
            latest[identity] = i
            next_at[identity] = data['at']
        if not changed:
            return
        rewritten, previous = [], ''
        for i, data in enumerate(data_rows, 1):
            body = {'seq':i, 'previous':previous, 'data':data}
            previous = hashlib.sha256(packed(body).encode()).hexdigest()
            rewritten.append({**body, 'hash':previous})
        old_head = {'seq':len(records), 'hash':records[-1]['hash']}
        new_head = {'seq':len(rewritten), 'hash':previous}
        atomic(self.head, packed({'rotating':[old_head,new_head]}).encode())
        atomic(self.log, (''.join(packed(row)+'\n' for row in rewritten)).encode())
        atomic(self.head, packed(new_head).encode())

    def status(self):
        with self.lock():
            self._sync()
            day = datetime.fromtimestamp(self.clock(), TZ).date().isoformat()
            return {'ready': True, 'maintenance_slot': self.meta('maintenance_slot'),
                    'maintenance_success': self.meta('maintenance_success'),
                    'maintenance_error': self.meta('maintenance_error'),
                    'compression_pending': self.db.execute('SELECT count(*) FROM facts WHERE deleted=0 '
                        'AND compacted<revision').fetchone()[0],
                    'budgets': dict(self.db.execute('SELECT kind,calls FROM budgets WHERE day=?', (day,)))}
