import asyncio
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

from home_agent.database import Database
from home_agent.routing import LocalRouter
from home_agent.supervision import Supervisor, enqueue_review


async def main():
    with tempfile.TemporaryDirectory(prefix="qwen-supervision-") as root:
        root = Path(root)
        caps = [
            {
                "id": "switch:example:" + a,
                "service": "switch",
                "target": "example",
                "action": a,
                "aliases": ["example lights"],
                "label": a + " example lights",
                "mutation": a != "status",
            }
            for a in ("on", "off", "status")
        ]
        catalog = {"version": "synthetic-v1", "capabilities": caps}
        (root / "catalog.json").write_text(json.dumps(catalog))
        (root / "routing.json").write_text(
            json.dumps(
                {
                    "catalog": str(root / "catalog.json"),
                    "port": 18081,
                    "executor": ["/usr/bin/false"],
                    "inference_timeout_seconds": 2,
                }
            )
        )
        db = Database(root / "test.sqlite3")
        db.initialize()
        router = LocalRouter(root / "routing.json", db)
        job = db.enqueue("telegram", "Please illuminate the example lamps")
        job = db.claim_next()
        enqueue_review(
            db, job, {"outcome": "fallback", "reason": "unrecognized_home_target"}, {}, catalog
        )
        db.finish(job.id, "completed", response="Simulated takeover")
        before = router.candidates(job.prompt)

        class Reviewer:
            async def run(self, *_, **__):
                return SimpleNamespace(
                    response=json.dumps(
                        {
                            "verdict": "incorrect",
                            "expected_capability": "switch:example:on",
                            "explanation": "Illuminate means turn on the example lights.",
                        }
                    )
                )

        supervisor = Supervisor(db, router, Reviewer(), lambda: None)
        with db.connect() as conn:
            row = dict(conn.execute("SELECT * FROM local_reviews").fetchone())
        await supervisor.process(row)
        with db.connect() as conn:
            review = dict(conn.execute("SELECT status,result,error FROM local_reviews").fetchone())
        after = router.candidates(job.prompt)
        decision = router.classify(job.prompt, after) if after else {}
        print(
            json.dumps(
                {
                    "before_candidates": len(before),
                    "review": review,
                    "after_candidates": len(after),
                    "next_decision": decision,
                    "device_commands_executed": 0,
                }
            ),
            flush=True,
        )
        assert (
            review["status"] == "completed"
            and json.loads(review["result"])["learning"] == "promoted"
        )
        assert after[decision["op"] - 1]["id"] == "switch:example:on"


asyncio.run(main())
