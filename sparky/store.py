import hashlib
import json
import sqlite3
import time
import os
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from contextlib import contextmanager
from pathlib import Path
from . import runtime, language, brain, diagnostics, planning, segments, care

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
        self.summary_pool=ThreadPoolExecutor(max_workers=2,thread_name_prefix="sparky-summary")
        self.summary_lock=Lock()
        self.summary_pending={}
        self.summary_running=set()
        self.closed=False
        with self.db() as db:
            db.executescript("""
              PRAGMA journal_mode=WAL;
              CREATE TABLE IF NOT EXISTS profiles(id TEXT PRIMARY KEY, family TEXT NOT NULL, name TEXT NOT NULL, snapshot TEXT NOT NULL);
              CREATE INDEX IF NOT EXISTS family_index ON profiles(family);
              CREATE TABLE IF NOT EXISTS events(profile TEXT REFERENCES profiles ON DELETE CASCADE, id TEXT, digest TEXT NOT NULL, PRIMARY KEY(profile,id));
              CREATE TABLE IF NOT EXISTS observations(profile TEXT REFERENCES profiles ON DELETE CASCADE, event TEXT, timestamp INTEGER, record TEXT NOT NULL, PRIMARY KEY(profile,event));
              CREATE TABLE IF NOT EXISTS memories(profile TEXT REFERENCES profiles ON DELETE CASCADE, id TEXT, value TEXT NOT NULL, PRIMARY KEY(profile,id));
              CREATE TABLE IF NOT EXISTS traces(profile TEXT REFERENCES profiles ON DELETE CASCADE, turn TEXT, created REAL, data TEXT NOT NULL, PRIMARY KEY(profile,turn));
              CREATE TABLE IF NOT EXISTS summaries(profile TEXT PRIMARY KEY REFERENCES profiles ON DELETE CASCADE, epoch INTEGER, revision INTEGER, data TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS cancellations(profile TEXT REFERENCES profiles ON DELETE CASCADE, turn TEXT, created REAL, PRIMARY KEY(profile,turn));
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
        state=json.loads(row[0])
        summary=db.execute("SELECT epoch,revision,data FROM summaries WHERE profile=?",(pid,)).fetchone()
        state["conversation_summary"]=json.loads(summary["data"]) if summary and summary["epoch"]==state.get("context_epoch",0) else {}
        state["summary_revision"]=summary["revision"] if summary and summary["epoch"]==state.get("context_epoch",0) else 0
        return state

    def save(self,db,pid,state):
        db.execute("UPDATE profiles SET snapshot=? WHERE id=?",(dump(state),pid))

    def state(self,family,pid):
        with self.db() as db:
            return runtime.public(self.load(db,family,pid))

    def event(self,family,pid,event):
        started=time.monotonic()
        digest=hashlib.sha256(dump(event).encode()).hexdigest()
        def check(db):
            state=self.load(db,family,pid)
            if db.execute("SELECT 1 FROM cancellations WHERE profile=? AND turn=?",(pid,event["id"])).fetchone():
                raise Conflict("本轮已取消。")
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
        # Keep unsummarized turns while the background worker is catching up.
        # A slow summary must not create a gap between summary and recent text.
        recent=[t for t in history if t.get("revision",0)>state.get("summary_revision",0) or t in history[-8:]]
        with diagnostics.capture() as details:
            decision=runtime.propose(state,event,memory,recent,learning,self.catalog)
        brain_ms=diagnostics.elapsed(started)
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            state,replayed=check(db)
            if replayed:
                return runtime.public(state),True
            records=runtime.advance(state,event,self.catalog,decision)
            planning.finalize(state,decision)
            if decision[0] in {"refuse","help","meaning","pause","finish","language"}:
                active=state["active"]
                example=None
                if decision[0] in {"help","meaning"} and active and active["hint"]:
                    item=next(i for i in active["lesson"]["items"] if i["id"]==active["hint"]["item"])
                    example=item["forms"][0] if decision[0]=="help" else item["zh"]
                state["session_notes"]=(state.get("session_notes",[])+[{
                    "revision":state["revision"],"action":decision[0],
                    "lesson":(state["active"] or state["paused"] or {}).get("lesson",{}).get("id"),
                    "reply_id":state["reply"]["id"],"demonstration":example,"delivery":"pending"}])[-12:]
            for record in records:
                record.update(source=event.get("source",event["type"]), confidence=event.get("confidence"), revision=state["revision"])
                db.execute("INSERT INTO observations VALUES(?,?,?,?)",(pid,event["id"],int(time.time()),dump(record)))
            self.save(db,pid,state)
            db.execute("INSERT INTO events VALUES(?,?,?)",(pid,event["id"],digest))
            trace={"revision":state["revision"],"action":decision[0],"brain_ms":brain_ms,"decision":details,
                   "reply_id":state["reply"]["id"],"segments":len(state["reply"]["segments"])}
            if os.getenv("SPARKY_DEBUG_TRANSCRIPTS")=="1":
                trace.update(input=event.get("text",""),reply=state["reply"]["text"])
            self._trace(db,pid,event["id"],trace)
            if event["type"]=="utterance" and event.get("speaker","child")=="child" and event.get("confidence",1)>=0.55:
                with self.history_lock:
                    current=self.history.get(pid,(now,[]))[1]
                    self.history[pid]=(time.monotonic(),(current+[{"revision":state["revision"],"user":event["text"],
                        "reply":state["reply"]["text"],"reply_id":state["reply"]["id"],
                        "care":bool(state.get("care_kind")),
                        "delivery":"pending","segments":deepcopy(state["reply"]["segments"])}])[-32:])
        self.schedule_summary(family,pid,state)
        return runtime.public(state),False

    def cancel(self,family,pid,turn):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            self.load(db,family,pid)
            db.execute("INSERT OR REPLACE INTO cancellations VALUES(?,?,?)",(pid,turn,time.time()))
            db.execute("DELETE FROM cancellations WHERE profile=? AND turn NOT IN (SELECT turn FROM cancellations WHERE profile=? ORDER BY created DESC LIMIT 128)",(pid,pid))

    def current_turn(self,family,pid,turn,reply=None):
        with self.db() as db:
            state=self.load(db,family,pid)
            if db.execute("SELECT 1 FROM cancellations WHERE profile=? AND turn=?",(pid,turn)).fetchone():
                raise Conflict("本轮已取消。")
            if reply is not None and state["reply"]["id"]!=reply:
                raise Conflict("回复已过期。")
            return runtime.public(state)

    def delivery(self,family,pid,reply,status,attempt,segment=None):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            state=self.load(db,family,pid)
            current=state["reply"]
            if current["id"]!=reply or current["delivery"]=="complete":
                return
            segments.attach(current)
            target=current
            if segment is not None:
                if type(segment) is not int or not 0<=segment<len(current["segments"]):
                    raise ValueError("无效语音分句。")
                target=current["segments"][segment]
                if target["delivery"]=="complete":
                    return
            if status=="started":
                # A piggybacked receipt and its background request may arrive twice.
                if target.get("attempt")==attempt:
                    return
                target["attempt"]=attempt
            elif status=="failed" and target["delivery"]=="pending":
                target["attempt"]=attempt
            elif target.get("attempt")!=attempt or target["delivery"]!="started":
                return
            target["delivery"]=status
            if segment is None:
                for part in current["segments"]:
                    part["delivery"]=status
            else:
                phases=[s["delivery"] for s in current["segments"]]
                current["delivery"]=("complete" if all(p=="complete" for p in phases) else
                    "failed" if "failed" in phases else "interrupted" if "interrupted" in phases else "started")
            for activity in (state["active"],state["paused"]):
                if activity and activity["hint"] and activity["hint"]["id"]==reply:
                    required=activity["hint"].get("segments",[])
                    phases=[current["segments"][i]["delivery"] for i in required]
                    activity["hint"]["delivery"]=("complete" if phases and all(p=="complete" for p in phases) else current["delivery"])
            for note in state.get("session_notes",[]):
                if note["reply_id"]==reply:
                    taught=note.get("demonstration")
                    parts=[p for p in current["segments"] if taught and taught in p["text"]]
                    note["delivery"]="complete" if parts and all(p["delivery"]=="complete" for p in parts) else current["delivery"]
            self.save(db,pid,state)
            row=db.execute("SELECT turn FROM traces WHERE profile=? AND json_extract(data,'$.reply_id')=?",(pid,reply)).fetchone()
            if row:
                self._trace(db,pid,row["turn"],{"delivery":current["delivery"],"segment_delivery":[s["delivery"] for s in current["segments"]]})
            with self.history_lock:
                for turn in self.history.get(pid,(0,[]))[1]:
                    if turn["reply_id"]==reply:
                        turn.update(delivery=current["delivery"],segments=deepcopy(current["segments"]))

    def parent(self,family,pid,op="summary",value=""):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            state=self.load(db,family,pid)
            if op in {"delete","forget","forget_context"}:
                with self.history_lock:
                    self.history.pop(pid,None)
                runtime.say(state,language.wording(state,"We can keep playing or take a break.","我们可以继续玩，也可以休息。"))
                state["context_epoch"]=state.get("context_epoch",0)+1
                state["session_notes"]=[]
                state["conversation_summary"]={}
                db.execute("DELETE FROM summaries WHERE profile=?",(pid,))
                db.execute("DELETE FROM traces WHERE profile=?",(pid,))
            if op=="delete":
                db.execute("DELETE FROM profiles WHERE id=?",(pid,))
                return {}
            if op=="remember":
                db.execute("INSERT INTO memories VALUES(?,?,?)",(pid,runtime.uid(),value))
            elif op=="forget":
                db.execute("DELETE FROM memories WHERE profile=? AND id=?",(pid,value))
            elif op=="learner":
                state["learner"]=care.validate_learner(value)
            if op in {"forget","remember","forget_context","learner"}:
                # Invalidate an in-flight response based on a previous memory.
                state["revision"]+=1
                self.save(db,pid,state)
            rows=db.execute("""SELECT json_extract(record,'$.label') AS goal,
                    json_extract(record,'$.kind') AS kind, count(*) AS n
                    FROM observations WHERE profile=? GROUP BY goal,kind ORDER BY goal,kind""",(pid,))
            return {"learner":care.learner(state),"conversation_summary":state.get("conversation_summary",{}),
                    "observations":[dict(r) for r in rows],"memories":[dict(r) for r in db.execute(
                "SELECT id,value FROM memories WHERE profile=? ORDER BY rowid",(pid,))]}

    def _trace(self,db,pid,turn,values):
        old=db.execute("SELECT data FROM traces WHERE profile=? AND turn=?",(pid,turn)).fetchone()
        data=json.loads(old[0]) if old else {}
        data.update(values)
        db.execute("INSERT INTO traces VALUES(?,?,?,?) ON CONFLICT(profile,turn) DO UPDATE SET data=excluded.data",
                   (pid,turn,time.time(),dump(data)))
        db.execute("DELETE FROM traces WHERE profile=? AND turn NOT IN (SELECT turn FROM traces WHERE profile=? ORDER BY created DESC LIMIT 100)",(pid,pid))

    def trace(self,family,pid,turn,values):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            self.load(db,family,pid)
            self._trace(db,pid,turn,values)

    def debug(self,family,pid):
        with self.db() as db:
            state=self.load(db,family,pid)
            rows=db.execute("SELECT turn,created,data FROM traces WHERE profile=? ORDER BY created DESC LIMIT 30",(pid,)).fetchall()
        return {"turns":[{"turn":r["turn"],"created":r["created"],**json.loads(r["data"])} for r in rows],
                "summary":state.get("conversation_summary",{}),"notes":state.get("session_notes",[]),
                "transcripts":os.getenv("SPARKY_DEBUG_TRANSCRIPTS")=="1","prompt":brain.SYSTEM_PROMPT}

    def schedule_summary(self,family,pid,state):
        if not brain.configured() or self.closed or state.get("care_kind"):
            return
        with self.history_lock:
            history=deepcopy(self.history.get(pid,(0,[]))[1])
        # Checkpoint short conversations too, without putting summarization on
        # the speaking path. Recent turns remain available until summary commits.
        older=[t for t in history if t["revision"]>state.get("summary_revision",0) and not t.get("care")]
        if len(older)<2:
            return
        job=(family,state.get("context_epoch",0),state.get("summary_revision",0),older,state.get("session_notes",[]))
        with self.summary_lock:
            if self.closed:
                return
            self.summary_pending[pid]=job
            if pid not in self.summary_running:
                self.summary_running.add(pid)
                self.summary_pool.submit(self._summarize,pid)

    def _summarize(self,pid):
        while True:
            with self.summary_lock:
                job=self.summary_pending.pop(pid,None)
                if job is None or self.closed:
                    self.summary_running.discard(pid)
                    return
            family,epoch,_,older,notes=job
            try:
                with self.db() as db:
                    state=self.load(db,family,pid)
                if state.get("context_epoch",0)!=epoch:
                    continue
                base=state.get("summary_revision",0)
                older=[t for t in older if t["revision"]>base]
                if not older:
                    continue
                with diagnostics.capture() as details:
                    summary=brain.summarize(state.get("conversation_summary",{}),older,notes)
                with self.db() as db:
                    db.execute("BEGIN IMMEDIATE")
                    fresh=self.load(db,family,pid)
                    if fresh.get("context_epoch",0)!=epoch or fresh.get("summary_revision",0)!=base:
                        continue
                    if summary:
                        db.execute("INSERT OR REPLACE INTO summaries VALUES(?,?,?,?)",(pid,epoch,older[-1]["revision"],dump(summary)))
                    self._trace(db,pid,"summary-"+str(older[-1]["revision"]),{"summary_status":"updated" if summary else "failed","decision":details})
            except Missing:
                continue

    def close(self):
        with self.summary_lock:
            self.closed=True
            self.summary_pending.clear()
        self.summary_pool.shutdown(wait=False,cancel_futures=True)

