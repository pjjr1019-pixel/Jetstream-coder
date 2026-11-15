"""Locally provisioned PIN verifier. No bridge or HTTP setter exists."""
from contextlib import contextmanager
import hashlib,hmac,math,os,secrets,sqlite3,time
from pathlib import Path

ITERATIONS=600_000
LOCKOUT_SECONDS=900
DAY_SECONDS=86400

class UnlockError(ValueError):
    def __init__(self,code,message=None,*,cooldown_minutes=None):
        self.code=code;self.cooldown_minutes=cooldown_minutes
        super().__init__(message or code)

class PinStore:
    def __init__(self,home,clock=time.time):
        # Existing non-overridable operator.secret.* private-path rule applies.
        self.path=Path(home)/'operator.secret.remote-pin.sqlite3';self.clock=clock
        self.path.parent.mkdir(parents=True,exist_ok=True)
        try:fd=os.open(self.path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        except FileExistsError:pass
        else:os.close(fd)
        if os.name=='nt':
            from .cli import _lock_windows_file
            _lock_windows_file(self.path)
        else:os.chmod(self.path,0o600)
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS pin (id INTEGER PRIMARY KEY CHECK(id=1), salt BLOB NOT NULL, digest BLOB NOT NULL, iterations INTEGER NOT NULL, failures INTEGER NOT NULL, lockout REAL, revision TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS day_meta (id INTEGER PRIMARY KEY CHECK(id=1), generation INTEGER NOT NULL)')
            db.execute('INSERT OR IGNORE INTO day_meta VALUES (1,0)')
            db.execute('CREATE TABLE IF NOT EXISTS day_authorization (id INTEGER PRIMARY KEY CHECK(id=1), issued REAL NOT NULL, expires REAL NOT NULL, revision TEXT NOT NULL, generation INTEGER NOT NULL)')
    @contextmanager
    def db(self):
        db=sqlite3.connect(self.path,timeout=5)
        try:
            with db:yield db
        finally:db.close()
    @staticmethod
    def validate(pin):
        if not isinstance(pin,str) or not 6<=len(pin)<=128 or not pin.isascii() or not pin.isalnum():
            raise ValueError('Use 6–128 letters or digits for the PIN')
    def set_local(self,pin,confirmation):
        self.validate(pin)
        if not isinstance(confirmation,str) or not hmac.compare_digest(pin,confirmation):raise ValueError('The PIN entries do not match')
        salt=secrets.token_bytes(32);digest=hashlib.pbkdf2_hmac('sha256',pin.encode(),salt,ITERATIONS)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT OR REPLACE INTO pin VALUES (1,?,?,?,?,?,?)',(salt,digest,ITERATIONS,0,None,secrets.token_hex(16)))
            db.execute('DELETE FROM day_authorization')
            db.execute('UPDATE day_meta SET generation=generation+1 WHERE id=1')

    def day_generation(self):
        with self.db() as db:return db.execute('SELECT generation FROM day_meta WHERE id=1').fetchone()[0]

    def revoke_day(self):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            changed=db.execute('DELETE FROM day_authorization').rowcount>0
            # Advance even without an existing grant: STOP cancels in-flight verification.
            db.execute('UPDATE day_meta SET generation=generation+1 WHERE id=1')
        return changed

    def day_state(self):
        ended=None;state={'day_authorized':False,'day_authorization_expires_at':None}
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT d.issued,d.expires,d.revision,d.generation,p.revision,m.generation FROM day_authorization d JOIN pin p ON p.id=d.id JOIN day_meta m ON m.id=d.id WHERE d.id=1').fetchone()
            if row:
                issued,expires,revision,generation,pin_revision,current=row;now=self.clock()
                if revision!=pin_revision or generation!=current:ended='local_pin_changed'
                elif not math.isfinite(now) or now<issued or now>=expires:ended='expired'
                elif expires-issued!=DAY_SECONDS:ended='invalid_authorization'
                else:state.update(day_authorized=True,day_authorization_expires_at=expires)
                if ended:
                    db.execute('DELETE FROM day_authorization');db.execute('UPDATE day_meta SET generation=generation+1 WHERE id=1')
        return state,ended

    def require_day(self):
        state,_=self.day_state()
        if not state['day_authorized']:raise UnlockError('day_authorization_required','A fresh PIN unlock is required')
        with self.db() as db:
            row=db.execute('SELECT d.revision,d.generation,d.issued,d.expires,m.generation,p.revision FROM day_authorization d JOIN day_meta m ON m.id=d.id JOIN pin p ON p.id=d.id WHERE d.id=1').fetchone()
        now=self.clock()
        if not row or row[1]!=row[4] or row[0]!=row[5] or not row[2]<=now<row[3]:
            raise UnlockError('day_authorization_required','A fresh PIN unlock is required')
        return row[0],row[1]

    def grant_day(self,revision,generation,issued):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            current=db.execute('SELECT p.revision,m.generation FROM pin p JOIN day_meta m ON m.id=p.id WHERE p.id=1').fetchone()
            if current!=(revision,generation):raise UnlockError('unlock_cancelled','Control state changed; request a fresh unlock')
            row=db.execute('SELECT issued,expires,revision,generation FROM day_authorization WHERE id=1').fetchone()
            now=self.clock()
            if row and row[2:]==(revision,generation) and row[0]<=now<row[1]:return False
            if not math.isfinite(issued) or not issued<=now<issued+DAY_SECONDS:raise UnlockError('unlock_cancelled')
            db.execute('INSERT OR REPLACE INTO day_authorization VALUES (1,?,?,?,?)',(issued,issued+DAY_SECONDS,revision,generation))
        return True
    def public(self):
        with self.db() as db:row=db.execute('SELECT lockout FROM pin WHERE id=1').fetchone()
        return dict(pin_set=row is not None,unlock_lockout_until=row[0] if row and row[0] and row[0]>self.clock() else None)
    def revision(self):
        with self.db() as db:row=db.execute('SELECT revision FROM pin WHERE id=1').fetchone()
        return row[0] if row else None
    def verify(self,pin):
        # BEGIN IMMEDIATE serializes failed attempts across threads/processes,
        # including local PIN changes. A restart never resets the lockout.
        error=None;revision=None
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT salt,digest,iterations,failures,lockout,revision FROM pin WHERE id=1').fetchone()
            now=self.clock()
            if row is None:raise UnlockError('no_pin_set')
            salt,digest,iterations,failures,lockout,revision=row
            if lockout and lockout>now:
                raise UnlockError('unlock_lockout',cooldown_minutes=max(1,math.ceil((lockout-now)/60)))
            if lockout:failures=0
            # Untrusted malformed input still costs one attempt, with a fixed
            # bounded KDF input and exactly the same comparison/error shape.
            valid=isinstance(pin,str) and 6<=len(pin)<=128 and pin.isascii() and pin.isalnum()
            supplied=pin.encode() if valid else b'invalid bounded input'
            actual=hashlib.pbkdf2_hmac('sha256',supplied,salt,iterations)
            if hmac.compare_digest(actual,digest) and valid:
                db.execute('UPDATE pin SET failures=0,lockout=NULL WHERE id=1')
            else:
                failures+=1;until=now+LOCKOUT_SECONDS if failures>=5 else None
                db.execute('UPDATE pin SET failures=?,lockout=? WHERE id=1',(failures,until))
                error=UnlockError('unlock_lockout',cooldown_minutes=15) if until else UnlockError('invalid_pin')
        # Raise after committing the failure count, not inside the transaction.
        if error:raise error
        return revision
