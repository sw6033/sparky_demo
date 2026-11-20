"""Validate all configured lessons or replay an activity through the runtime."""
import argparse
from pathlib import Path
import tempfile
import uuid
from .content import Catalog, ROOT
from .store import Store

def demo(catalog, lesson_id):
    lesson=catalog.lessons[lesson_id]
    with tempfile.TemporaryDirectory() as folder:
        path=Path(folder)/"demo.sqlite3"
        engine=Store(path,catalog)
        pid=engine.create("demo","示例档案")
        def send(kind,**data):
            rev=engine.state("demo",pid)["revision"]
            state,_=engine.event("demo",pid,dict(id=uuid.uuid4().hex,revision=rev,type=kind,**data))
            print(f"{state['revision']:02}  {kind:10}  {state['reply']['text']}")
            return state
        state=send("start",lesson=lesson_id,mode="B")
        rules=lesson["rules"]["B"]["steps"]
        for index,spec in enumerate(rules):
            if index==1:
                send("pause")
                engine=Store(path,catalog)
                state=send("resume")
            if spec.get("expected"):
                send("select",item=lesson["targets"][spec["expected"]])
            else:
                item_id=state["active"]["options"][-1]["id"]
                item=next(i for i in lesson["items"] if i["id"]==item_id)
                value=item["en"] if index==0 and spec["accept"]=="meaning" else item["forms"][0]
                state=send("utterance",text=value)
        for row in engine.parent("demo",pid)["observations"]:
            print(f"    {row['goal']} · {row['kind']} × {row['n']}")

def main():
    parser=argparse.ArgumentParser(description="Sparky 课程配置与本地流程验证")
    parser.add_argument("command",choices=("check","demo"))
    parser.add_argument("lesson",nargs="?",default="animals")
    parser.add_argument("--curriculum",type=Path,default=ROOT/"curriculum")
    args=parser.parse_args()
    catalog=Catalog(args.curriculum)
    if args.command=="demo":
        if args.lesson not in catalog.lessons:
            parser.error("未知课程")
        demo(catalog,args.lesson)
    else:
        for lesson in catalog.lessons.values():
            print(f"{lesson['id']:12} v{lesson['version']}  {lesson['title']}  {lesson['digest']}")

if __name__=="__main__":
    main()

