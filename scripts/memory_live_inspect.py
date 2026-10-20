"""Read-only offline inspection of a saved live run; makes no API calls."""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.memory.summary import SummaryGenerator
from app.memory.types import MemoryUpdate


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("run_id")
    args=parser.parse_args()
    if Path(args.run_id).name != args.run_id or args.run_id in {".",".."}:
        parser.error("run_id must be one directory name")
    directory=ROOT/"tmp"/"memory-live"/args.run_id
    result=json.loads((directory/"result.json").read_text(encoding="utf-8"))
    calls=[json.loads(line) for line in (directory/"calls.jsonl").read_text(encoding="utf-8").splitlines()]
    db=sqlite3.connect((directory/"memory.db").as_uri()+"?mode=ro",uri=True)
    db.row_factory=sqlite3.Row
    sources={row["public_id"]:dict(row) for row in db.execute("SELECT * FROM chat_messages")}
    inspected=[]
    for call in calls:
        if not call["purpose"].startswith("memory_"):
            continue
        info=dict(number=call["number"],purpose=call["purpose"],api_status=call["status"],seconds=call["elapsed_seconds"])
        if call["status"]!="ok":
            info["failure"]=call.get("code",call.get("error"))
            inspected.append(info)
            continue
        payload=json.loads(call["messages"][-1]["content"])
        allowed_ids={m["message_id"] for m in payload["messages"]}
        allowed_ids.update(mid for item in payload["old_summary"] for mid in item["source_message_ids"])
        allowed_ids.update(e["message_id"] for item in payload["related_memories"] for e in item["evidence"])
        if payload.get("current_message"):
            allowed_ids.add(payload["current_message"]["message_id"])
        allowed={mid:SimpleNamespace(**sources[mid]) for mid in allowed_ids if mid in sources}
        related=[SimpleNamespace(public_id=r["memory_id"],version=r["version"],
                    status=r["status"],evidence_json=json.dumps(r["evidence"])) for r in payload["related_memories"]]
        try:
            raw=call["response"]["content"].strip()
            if raw.startswith("```"):
                raw="\n".join(raw.splitlines()[1:-1])
            update=MemoryUpdate.model_validate_json(raw)
            if update.needs_clarification and update.memory_operations:
                raise ValueError("ambiguous references cannot mutate memory")
            SummaryGenerator.validate(update,allowed,related)
            if sum(len(x.text)+1 for x in update.summary_items)>payload["summary_max_chars"]:
                raise ValueError("summary exceeds length limit")
            if payload["mode"]=="compact":
                covered={mid for item in [*update.summary_items,*update.memory_operations] for mid in item.source_message_ids}
                covered.update(e["message_id"] for r in payload["related_memories"] for e in r["evidence"])
                lost=[item for item in payload["old_summary"] if not set(item["source_message_ids"])<=covered]
                if lost:
                    info["lost_summary_items"]=lost
                    raise ValueError("old summary evidence would be silently discarded")
            info["validation"]="passed"
        except Exception as exc:
            info["validation"]="failed"
            info["failure"]=str(exc)
        inspected.append(info)
    summary=dict(status=result.get("status","running"),last_snapshot=result["snapshots"][-1]["label"],
                 api_calls=len(calls),api_status_counts=dict(Counter(c["status"] for c in calls)),
                 api_purposes=dict(Counter(c["purpose"] for c in calls)),
                 memory_job_status_counts=dict(Counter(j["status"] for j in result["memory_calls"])),
                 failed_checks=[x for x in result["checks"] if not x["passed"]],
                 check_count=len(result["checks"]),usage=result["usage"],
                 incomplete_snapshots=[s["label"] for s in result["snapshots"] if s["context"]["background_incomplete"]],
                 memory_validation=inspected,probes=[dict(id=p["id"],question=p["question"],answer=p.get("answer"),
                    error=p.get("error"),summary=p["context"]["memory_brief"],
                    recalled=[m["content"] for m in p["context"]["recalled_memories"]],
                    history_ids=[m["db_id"] for m in p["context"]["model_history"]]) for p in result["probes"]])
    path=directory/"inspection.json"
    path.write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding="utf-8")
    print(path)
    db.close()


if __name__=="__main__":
    main()
