"""Offline recovery only. Revocation journal/witness must remain current."""
import argparse
import fcntl
import os
import sqlite3
from pathlib import Path

from .store import SharedMemory, MemoryUnavailable


def restore(directory, backup):
    directory, backup = Path(directory), Path(backup)
    if not backup.is_file() or not (directory/'memory.db').is_file():
        raise MemoryUnavailable('请保留当前记忆目录、撤销日志与见证；备份必须为已有文件')
    with open(directory/'runtime.lock', 'a+b') as runtime:
        try:
            fcntl.flock(runtime, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise MemoryUnavailable('请先停止 QQ 与微信目标实例，再恢复共享记忆') from None
        candidate = directory/'.restore-candidate.db'
        candidate.unlink(missing_ok=True)
        try:
            with sqlite3.connect(f'file:{backup}?mode=ro', uri=True) as source, sqlite3.connect(candidate) as target:
                source.backup(target)
            os.chmod(candidate, 0o600)
            recovered = SharedMemory.__new__(SharedMemory)
            recovered.directory = directory
            recovered.log, recovered.head = directory/'revocations.jsonl', directory/'journal-head.json'
            recovered.db = sqlite3.connect(candidate, isolation_level=None)
            recovered.db.row_factory = sqlite3.Row
            try:
                recovered.db.execute('PRAGMA journal_mode=DELETE')
                recovered.db.execute('PRAGMA synchronous=FULL')
                if recovered.db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise MemoryUnavailable('备份完整性校验失败')
                with recovered.lock():
                    recovered._sync()  # Fail closed BEFORE replacing the active database.
            finally:
                recovered.db.close()
            with sqlite3.connect(directory/'memory.db') as source, \
                    sqlite3.connect(directory/'before-restore.db') as safety:
                source.backup(safety)
            for suffix in ('-wal', '-shm'):
                (directory/('memory.db'+suffix)).unlink(missing_ok=True)
            os.replace(candidate, directory/'memory.db')
            fd = os.open(directory, os.O_DIRECTORY | os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            candidate.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description='共享长期记忆离线恢复；不会恢复撤销日志')
    parser.add_argument('action', choices=['restore'])
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--backup', type=Path, required=True)
    args = parser.parse_args()
    try:
        restore(args.directory, args.backup)
    except Exception as exc:
        raise SystemExit(f'恢复未启用：{type(exc).__name__}。请检查运行实例、备份和完整撤销日志。') from None
    print('共享库已恢复并重放全部撤销记录；可以启动目标实例。')


if __name__ == '__main__':
    main()
