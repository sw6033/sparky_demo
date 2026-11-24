"""CLI entry point: validate configurable curriculum or run a multi-session POC demo."""
from __future__ import annotations

import argparse
import tempfile
import uuid
from pathlib import Path

from . import brain
from .curriculum import Curriculum, ROOT
from .store import Store


def run_demo(curriculum: Curriculum) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "demo.sqlite3", curriculum)
        state = store.create_profile("Leo", preset="fresh")
        pid = state["profile"]["id"]
        print(f"=== Session {state['session']['session_num']} Opening ===")
        print(f"Sparky: {state['session']['reply']['text']}\n")

        script = [
            "Hi! My name is Leo and I built a giant dinosaur today!",
            "Yeah, it has huge teeth and rockets on its back!",
            "Ooh cool! How do I say hello and dinosaur?",
            "你好，我是 Leo!",
            "恐龙!",
            "我喜欢恐龙!",
        ]
        for user_text in script:
            turn_id = uuid.uuid4().hex
            ctx = store.prepare_turn_context(pid, turn_id, user_text)
            reply_text = ""
            meta: dict = {}
            for kind, *payload in brain.stream_turn(user_text, ctx):
                if kind == "done":
                    reply_text, meta, _ = payload
            state = store.commit_turn(pid, turn_id, user_text, "text", reply_text, meta, ctx["focus"])
            store.record_delivery(pid, state["session"]["reply"]["id"], "complete")
            cards_str = ", ".join(f"[{c['emoji']} {c['zh']}]" for c in state["session"]["ui_cards"])
            print(f"Child : {user_text}")
            print(f"Sparky: {reply_text}  (move={meta.get('move')}) {cards_str}\n")

        next_state = store.start_next_session(
            pid,
            summarizer=brain.summarize_session,
            opening_generator=brain.generate_session_opening,
        )
        print(f"=== Simulated Next Day: Session {next_state['session']['session_num']} Opening ===")
        print(f"Sparky: {next_state['session']['reply']['text']}")
        print(f"Active Unit: {next_state['inspector']['progress']['active_unit_id']}")
        print("Unlocked Stars:", ", ".join(f"{s['emoji']}{s['zh']}({s['stage']})" for s in next_state["stars"]))


def main() -> None:
    parser = argparse.ArgumentParser(description="Sparky v2 课程大纲校验与跨场陪伴教学演示")
    parser.add_argument("command", choices=("check", "demo"), nargs="?", default="check")
    parser.add_argument("--curriculum", type=Path, default=ROOT / "curriculum")
    args = parser.parse_args()

    curriculum = Curriculum(args.curriculum)
    if args.command == "demo":
        run_demo(curriculum)
        return

    summary = curriculum.summary()
    print(f"{summary['title']}  (age {summary['age_band']}, digest {summary['digest']})")
    print(f"Stages: {' -> '.join(summary['stages'])}")
    for u in summary["units"]:
        prereq = ", ".join(u["prerequisites"]) or "none"
        print(
            f"  [{u['order']}] {u['id']:16} {u['title']} ({u['title_zh']}) | "
            f"patterns={len(u['patterns'])} vocab={len(u['vocabulary'])} playlets={len(u['playlets'])} prereq={prereq}"
        )


if __name__ == "__main__":
    main()
