import hashlib
import json
import sqlite3
import time
from threading import Lock
from contextlib import contextmanager
from pathlib import Path
from . import runtime, language

def dump(value):
    return json.dumps(value,ensure_ascii=False,separators=(",",":"),sort_keys=True)

class Conflict(Exception):
    pass

class Missing(Exception):
    pass

class Store:
    def __init__(self,path,catalog):
        self.path=Path(path)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.catalog=catalog
        self.history={}
        self.history_lock=Lock()
        with self.db() as db:
            db.executescript("""
              PRAGMA journal_mode=WAL;
              CREATE TABLE IF NOT EXISTS profiles(id TEXT PRIMARY KEY, family TEXT NOT NULL, name TEXT NOT NULL, snapshot TEXT NOT NULL);
              CREATE INDEX IF NOT EXISTS family_index ON profiles(family);
              CREATE TABLE IF NOT EXISTS events(profile TEXT REFERENCES profiles ON DELETE CASCADE, id TEXT, digest TEXT NOT NULL, PRIMARY KEY(profile,id));
              CREATE TABLE IF NOT EXISTS observations(profile TEXT REFERENCES profiles ON DELETE CASCADE, event TEXT, timestamp INTEGER, record TEXT NOT NULL, PRIMARY KEY(profile,event));
              CREATE TABLE IF NOT EXISTS memories(profile TEXT REFERENCES profiles ON DELETE CASCADE, id TEXT, value TEXT NOT NULL, PRIMARY KEY(profile,id));
            """)
    @contextmanager
    def db(self):
        db=sqlite3.connect(self.path,timeout=8)
        db.row_factory=sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def create(self,family,name):
        pid=runtime.uid()
        with self.db() as db:
            db.execute("INSERT INTO profiles VALUES(?,?,?,?)",(pid,family,name,dump(runtime.initial())))
        return pid

    def profiles(self,family):
        with self.db() as db:
            return [dict(x) for x in db.execute("SELECT id,name FROM profiles WHERE family=? ORDER BY rowid",(family,))]

    def load(self,db,family,pid):
        row=db.execute("SELECT snapshot FROM profiles WHERE family=? AND id=?",(family,pid)).fetchone()
        if not row:
            raise Missing()
        return json.loads(row[0])

    def save(self,db,pid,state):
        db.execute("UPDATE profiles SET snapshot=? WHERE id=?",(dump(state),pid))

    def state(self,family,pid):
        with self.db() as db:
            return runtime.public(self.load(db,family,pid))

    def event(self,family,pid,event):
        digest=hashlib.sha256(dump(event).encode()).hexdigest()
        def check(db):
            state=self.load(db,family,pid)
            old=db.execute("SELECT digest FROM events WHERE profile=? AND id=?",(pid,event["id"])).fetchone()
            if old and old[0]!=digest:
                raise Conflict("事件编号已用于其他输入。")
            if not old and state["revision"]!=event["revision"]:
                raise Conflict("进度已更新，请重试。")
            return state,bool(old)
        with self.db() as db:
            state,replayed=check(db)
            if replayed:
                return runtime.public(state),True
            memory=[r[0] for r in db.execute("SELECT value FROM memories WHERE profile=? ORDER BY rowid DESC LIMIT 4",(pid,))]
            learning=[json.loads(r[0]) for r in db.execute("SELECT record FROM observations WHERE profile=? ORDER BY rowid DESC LIMIT 4",(pid,))]
        with self.history_lock:
            now=time.monotonic()
            self.history={k:v for k,v in self.history.items() if now-v[0]<900}
            history=self.history.get(pid,(now,[]))[1]
        decision=runtime.propose(state,event,memory,history,learning,self.catalog)
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            state,replayed=check(db)
            if replayed:
                return runtime.public(state),True
            records=runtime.advance(state,event,self.catalog,decision)
            for record in records:
                record.update(source=event.get("source",event["type"]), confidence=event.get("confidence"), revision=state["revision"])
                db.execute("INSERT INTO observations VALUES(?,?,?,?)",(pid,event["id"],int(time.time()),dump(record)))
            self.save(db,pid,state)
            db.execute("INSERT INTO events VALUES(?,?,?)",(pid,event["id"],digest))
            if event["type"]=="utterance" and event.get("speaker","child")=="child" and event.get("confidence",1)>=0.55:
                with self.history_lock:
                    self.history[pid]=(time.monotonic(),(history+[{"user":event["text"],"reply":state["reply"]["text"]}])[-4:])
        return runtime.public(state),False

    def delivery(self,family,pid,reply,status,attempt):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            state=self.load(db,family,pid)
            current=state["reply"]
            if current["id"]!=reply or current["delivery"]=="complete":
                return
            if status=="started":
                current["attempt"]=attempt
            elif current.get("attempt")!=attempt or current["delivery"]!="started":
                return
            current["delivery"]=status
            for activity in (state["active"],state["paused"]):
                if activity and activity["hint"] and activity["hint"]["id"]==reply:
                    activity["hint"]["delivery"]=status
            self.save(db,pid,state)

    def parent(self,family,pid,op="summary",value=""):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            state=self.load(db,family,pid)
            if op in {"delete","forget"}:
                with self.history_lock:
                    self.history.pop(pid,None)
                runtime.say(state,language.wording(state,"We can keep playing or take a break.","我们可以继续玩，也可以休息。"))
            if op=="delete":
                db.execute("DELETE FROM profiles WHERE id=?",(pid,))
                return {}
            if op=="remember":
                db.execute("INSERT INTO memories VALUES(?,?,?)",(pid,runtime.uid(),value))
            elif op=="forget":
                db.execute("DELETE FROM memories WHERE profile=? AND id=?",(pid,value))
            if op in {"forget","remember"}:
                # Invalidate an in-flight response based on a previous memory.
                state["revision"]+=1
                self.save(db,pid,state)
            rows=db.execute("""SELECT json_extract(record,'$.label') AS goal,
                    json_extract(record,'$.kind') AS kind, count(*) AS n
                    FROM observations WHERE profile=? GROUP BY goal,kind ORDER BY goal,kind""",(pid,))
            return {"observations":[dict(r) for r in rows],"memories":[dict(r) for r in db.execute(
                "SELECT id,value FROM memories WHERE profile=? ORDER BY rowid",(pid,))]}

