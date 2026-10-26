"""Original observation age survives locks and commit latency; synthetic SQLite."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import threading

from homeplane.people.live import IdentityStore


def store_at(tmp_path, clock):
    private=tmp_path.resolve()/'private';private.mkdir(mode=0o700)
    return IdentityStore(private/'identities.sqlite',clock=lambda:clock[0])


def test_presence_uses_the_original_source_time(tmp_path):
    clock=[1000.];store=store_at(tmp_path,clock)
    try:
        assert store.observe('camera','session','1','known','matched',source_age=lambda:.4)
        row=store.presence()[0]
        assert row['last_seen']==999.6 and row['present']
    finally:store.close()


def test_sqlite_write_lock_wait_cannot_refresh_an_expired_source(tmp_path):
    clock=[100.];store=store_at(tmp_path,clock)
    other=sqlite3.connect(store.path)
    other.execute('BEGIN IMMEDIATE')
    checked=threading.Event()
    def age():checked.set();return clock[0]-100.
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending=pool.submit(store.observe,'camera','session','1','known','matched',source_age=age)
            assert not checked.wait(.05)
            clock[0]=101.1;other.commit()
            assert pending.result(timeout=1) is False
        assert not any(row['present'] for row in store.presence())
    finally:other.close();store.close()


def test_commit_delay_retracts_current_presence_without_changing_schema(tmp_path):
    clock=[100.];store=store_at(tmp_path,clock)
    original=store.db
    class SlowCommit:
        delayed=False
        def __enter__(self):original.__enter__();return self
        def __exit__(self,*args):
            result=original.__exit__(*args)
            if not self.delayed:self.delayed=True;clock[0]+=1.1
            return result
        def __getattr__(self,name):return getattr(original,name)
    store.db=SlowCommit()
    try:
        assert store.observe('camera','session','1','known','matched',source_age=lambda:clock[0]-100.) is False
        assert not any(row['present'] for row in store.presence())
        assert len(list(original.execute('pragma table_info(presence)')))==7
    finally:store.close()
