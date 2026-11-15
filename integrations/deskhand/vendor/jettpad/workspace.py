"""Private, rebuildable offline search cache and bounded background workspace I/O."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time

from workspace_store import PROJECTION,organize


def connect_source(folder, write=False):
    path=(Path(folder)/'pad-state.sqlite3').resolve()
    db=sqlite3.connect(path.as_uri()+('?mode=rw' if write else '?mode=ro'),uri=True,timeout=1)
    db.row_factory=sqlite3.Row
    return db


def fuzzy_score(query, label):
    query=query.casefold().strip();label=label.casefold()
    if not query:return 1
    if query==label:return 1000
    if label.startswith(query):return 800-len(label)
    if query in label:return 600-label.index(query)
    cursor=0;gaps=0
    for char in query:
        found=label.find(char,cursor)
        if found<0:return 0
        gaps+=found-cursor;cursor=found+1
    return max(1,300-gaps-len(label))


def suggested_tags(text):
    rules=(('Windows',r'\b(windows|driver|settings|desktop)\b'),('Work',r'\b(job|work|schedule|shift)\b'),
           ('Ideas',r'\b(idea|suggest|could|improve)\b'),('Questions',r'\?'))
    return [tag for tag,pattern in rules if re.search(pattern,text,re.I)]


def query_messages(folder, query='', *, kind='all', sender='all', limit=30, cancelled=None):
    if not isinstance(query,str) or len(query)>160:raise ValueError('Search is limited to 160 characters')
    if kind not in ('all','pinned','attention') or sender not in ('all','phil','jett','offline','system'):
        raise ValueError('Invalid search filter')
    limit=max(1,min(int(limit),50));terms=re.findall(r'\w+',query,flags=re.UNICODE)[:8]
    with closing(connect_source(folder)) as db:
        if cancelled:db.set_progress_handler(lambda:int(cancelled()),1000)
        where=["m.status!='deferred_cloud'"];args=[]
        if kind=='pinned':where.append('x.pinned=1')
        if kind=='attention':where.append("o.state IN ('uncertain','offline_failed','offline_interrupted')")
        if sender!='all':where.append('m.sender=?');args.append(sender)
        indexed=False;identities=[]
        cache=Path(folder)/'workspace-index.sqlite3'
        if terms and cache.exists() and os.environ.get('JETTPAD_SEARCH_INDEX','1')!='0':
            try:
                with closing(sqlite3.connect(cache.resolve().as_uri()+'?mode=ro',uri=True,timeout=.2)) as index:
                    if cancelled:index.set_progress_handler(lambda:int(cancelled()),1000)
                    expression=' AND '.join('"'+term.replace('"','""')+'"*' for term in terms)
                    identities=[row[0] for row in index.execute('SELECT d.id FROM search s JOIN docs d ON d.rowid=s.rowid WHERE search MATCH ? ORDER BY bm25(search) LIMIT 120',(expression,))]
                    indexed=True
            except sqlite3.OperationalError as error:
                if cancelled and cancelled():return []
                indexed=False
        if identities:
            where.append('m.id IN ('+','.join('?' for _ in identities)+')');args.extend(identities)
        elif terms and indexed:
            # Bounded local fuzzy fallback, never a full history body scan.
            rows=[dict(r) for r in db.execute(PROJECTION+'WHERE '+' AND '.join(where)+' ORDER BY m.ts DESC,m.rowid DESC LIMIT 400',args)]
            ranked=sorted(((fuzzy_score(query,r['text'][:400]),r) for r in rows),key=lambda p:p[0],reverse=True)
            rows=[row for score,row in ranked if score][:limit]
            return decorate(rows,terms)
        elif terms:
            # Cache can be rebuilding; basic recent search stays available.
            for term in terms:
                where.append('m.text LIKE ? ESCAPE \'\\\'');args.append('%'+term.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%')
        rows=[dict(r) for r in db.execute(PROJECTION+'WHERE '+' AND '.join(where)+' ORDER BY m.ts DESC,m.rowid DESC LIMIT ?',(*args,len(identities) if identities else limit))]
        if identities:
            rank={value:i for i,value in enumerate(identities)}
            rows.sort(key=lambda r:rank.get(r['id'],999))
        return decorate(rows[:limit],terms)


def related_messages(folder,identity):
    with closing(connect_source(folder)) as db:
        original=db.execute(PROJECTION+"WHERE m.id=? AND m.status!='deferred_cloud'",(identity,)).fetchone()
        if original is None:return []
        digest=hashlib.sha256(' '.join(original['text'].casefold().split()).encode()).hexdigest();duplicates=[]
        try:
            cache=(Path(folder)/'workspace-index.sqlite3').resolve()
            with closing(sqlite3.connect(cache.as_uri()+'?mode=ro',uri=True,timeout=.2)) as index:
                duplicates=[row[0] for row in index.execute('SELECT id FROM docs WHERE digest=? AND id!=? LIMIT 20',(digest,identity))]
        except sqlite3.Error:pass
        rows=[]
        for value in duplicates:
            row=db.execute(PROJECTION+"WHERE m.id=? AND m.status!='deferred_cloud'",(value,)).fetchone()
            if row and ' '.join(row['text'].casefold().split())==' '.join(original['text'].casefold().split()):
                item=dict(row);item['relation']='Same normalized text';rows.append(item)
        words=sorted(set(re.findall(r'\w{5,}',original['text'].casefold())),key=len,reverse=True)[:3]
        seen={identity,*duplicates}
        for word in words:
            for item in query_messages(folder,word,limit=8):
                if item['id'] not in seen:
                    item['relation']='Shared word: '+word;rows.append(item);seen.add(item['id'])
                if len(rows)>=12:break
            if len(rows)>=12:break
        return decorate(rows[:12],[])


def decorate(rows,terms):
    for row in rows:
        text=row['text'];lower=text.casefold()
        places=[lower.find(term.casefold()) for term in terms if term.casefold() in lower]
        start=max(0,min(places)-45) if places else 0
        row['preview']=('…' if start else '')+text[start:start+220].replace('\n',' ')+('…' if len(text)>start+220 else '')
        row['tags']=json.loads(row['tags']) if isinstance(row.get('tags'),str) else row.get('tags',[])
        row['pinned']=bool(row['pinned'])
    return rows


class Workspace:
    def __init__(self,folder):
        self.folder=Path(folder);self.stop=threading.Event()
        self.jobs=ThreadPoolExecutor(max_workers=1,thread_name_prefix='Jett workspace query')
        self.indexed=0;self.total=0;self.ready=False;self.error=None
        self.index_thread=None
        if os.environ.get('JETTPAD_SEARCH_INDEX','1')!='0':
            self.index_thread=threading.Thread(target=self._index,daemon=True,name='Jett local search index');self.index_thread.start()
        else:self.ready=True

    def _index(self):
        try:
            with closing(connect_source(self.folder)) as source, closing(sqlite3.connect(self.folder/'workspace-index.sqlite3',timeout=1)) as db:
                db.execute('PRAGMA journal_mode=WAL')
                db.executescript('''
                    CREATE TABLE IF NOT EXISTS docs (id TEXT UNIQUE,text TEXT,sender TEXT,ts REAL,digest TEXT);
                    CREATE INDEX IF NOT EXISTS duplicate_content ON docs(digest);
                    CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(text,content='docs',content_rowid='rowid');
                    CREATE TABLE IF NOT EXISTS progress (key TEXT PRIMARY KEY,value TEXT);
                    CREATE TRIGGER IF NOT EXISTS docs_ai AFTER INSERT ON docs BEGIN INSERT INTO search(rowid,text) VALUES (new.rowid,new.text); END;
                    CREATE TRIGGER IF NOT EXISTS docs_ad AFTER DELETE ON docs BEGIN INSERT INTO search(search,rowid,text) VALUES ('delete',old.rowid,old.text); END;
                    CREATE TRIGGER IF NOT EXISTS docs_au AFTER UPDATE ON docs BEGIN
                        INSERT INTO search(search,rowid,text) VALUES ('delete',old.rowid,old.text);
                        INSERT INTO search(rowid,text) VALUES (new.rowid,new.text); END;
                ''')
                generation=source.execute("SELECT value FROM settings WHERE key='workspace_uuid'").fetchone()[0]
                saved=db.execute("SELECT value FROM progress WHERE key='generation'").fetchone()
                if not saved or saved[0]!=generation:
                    with db:
                        db.execute('DELETE FROM docs');db.execute('DELETE FROM progress')
                        db.execute("INSERT INTO progress VALUES ('generation',?)",(generation,))
                        initial_revision=source.execute('SELECT revision FROM chat_revision WHERE singleton=1').fetchone()[0]
                        db.execute("INSERT INTO progress VALUES ('revision',?)",(str(initial_revision),))
                def setting(key,default=0):
                    row=db.execute('SELECT value FROM progress WHERE key=?',(key,)).fetchone()
                    return int(row[0]) if row else default
                position=setting('position');revision=setting('revision')
                upper=source.execute('SELECT COALESCE(MAX(rowid),0) FROM messages').fetchone()[0]
                self.total=source.execute('SELECT COUNT(*) FROM messages').fetchone()[0]
                def add(row,identity):
                    if row is None or row['status']=='deferred_cloud':db.execute('DELETE FROM docs WHERE id=?',(identity,));return
                    text=row['text'];digest=hashlib.sha256(' '.join(text.casefold().split()).encode()).hexdigest()
                    db.execute('INSERT INTO docs VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET text=excluded.text,sender=excluded.sender,ts=excluded.ts,digest=excluded.digest WHERE docs.text IS NOT excluded.text OR docs.sender IS NOT excluded.sender OR docs.ts IS NOT excluded.ts',
                               (identity,text,row['sender'],row['ts'],digest))
                while not self.stop.is_set():
                    if position<upper:
                        rows=source.execute('SELECT *,rowid AS pos FROM messages WHERE rowid>? AND rowid<=? ORDER BY rowid LIMIT 100',(position,upper)).fetchall()
                        with db:
                            for row in rows:add(row,row['id'])
                            position=rows[-1]['pos'] if rows else upper
                            db.execute("INSERT OR REPLACE INTO progress VALUES ('position',?)",(str(position),))
                        self.indexed=db.execute('SELECT COUNT(*) FROM docs').fetchone()[0]
                    else:
                        self.ready=True;self.indexed=db.execute('SELECT COUNT(*) FROM docs').fetchone()[0]
                        changes=source.execute('SELECT id,revision FROM chat_changes WHERE revision>? ORDER BY revision LIMIT 100',(revision,)).fetchall()
                        if changes:
                            with db:
                                for item in changes:
                                    row=source.execute('SELECT * FROM messages WHERE id=?',(item['id'],)).fetchone()
                                    add(row,item['id']);revision=item['revision']
                                db.execute("INSERT OR REPLACE INTO progress VALUES ('revision',?)",(str(revision),))
                    self.stop.wait(.02 if not self.ready else .2)
        except Exception as error:
            self.error=type(error).__name__  # no body/query data in diagnostics

    def search(self,query='',**kwargs):
        cancelled=kwargs.pop('cancelled',None)
        return self.jobs.submit(query_messages,self.folder,query,cancelled=lambda:self.stop.is_set() or bool(cancelled and cancelled()),**kwargs)
    def organize(self,*args,**kwargs):return self.jobs.submit(organize,self.folder,*args,**kwargs)
    def related(self,identity):return self.jobs.submit(related_messages,self.folder,identity)

    def recent_queries(self,query=None):
        with closing(connect_source(self.folder,write=query is not None)) as db, db:
            row=db.execute("SELECT value FROM settings WHERE key='workspace_recents'").fetchone()
            values=json.loads(row[0]) if row else []
            if query:
                values=([query]+[v for v in values if v!=query])[:12]
                db.execute("INSERT OR REPLACE INTO settings VALUES ('workspace_recents',?)",(json.dumps(values),))
            return values

    def close(self):
        self.stop.set();self.jobs.shutdown(wait=True,cancel_futures=True)
        if self.index_thread:self.index_thread.join(2)


class DraftWriter:
    def __init__(self,folder):
        self.folder=folder;self.condition=threading.Condition();self.version=0;self.saved=0
        self.text='';self.error=None;self.stopped=False
        self.thread=threading.Thread(target=self._run,daemon=True,name='Jett draft autosave');self.thread.start()
    def submit(self,text):
        with self.condition:
            if text==self.text and self.version:return self.version
            self.text=text;self.version+=1;self.condition.notify_all();return self.version
    def _run(self):
        db=None
        try:
            while True:
                with self.condition:
                    self.condition.wait_for(lambda:self.stopped or self.version>self.saved)
                    if self.stopped and self.version<=self.saved:return
                    version,text=self.version,self.text
                try:
                    if db is None:db=connect_source(self.folder,write=True)
                    with db:db.execute("INSERT OR REPLACE INTO settings VALUES ('draft',?)",(text,))
                    with self.condition:self.saved=version;self.error=None;self.condition.notify_all()
                except Exception as error:
                    if db is not None:db.close();db=None
                    with self.condition:self.error=type(error).__name__;self.condition.notify_all()
                    if self.stopped:return
                    time.sleep(.2)
        finally:
            if db is not None:db.close()
    def flush(self,text,timeout=3):
        version=self.submit(text)
        with self.condition:
            if not self.condition.wait_for(lambda:self.saved>=version,timeout):raise OSError('Draft could not be saved; keep the app open')
    def close(self,text):
        self.flush(text)
        with self.condition:self.stopped=True;self.condition.notify_all()
        self.thread.join(2)
