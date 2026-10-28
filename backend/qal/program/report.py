"""Final report of a program: everything needed to audit it, exported with its configuration.

The report labels the mode on every result (historical replay, prospective paper, synthetic simulation), keeps the
whole period even after an early stop, lists every pause and review, and states its limitations.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from qal.models import Experiment, PaperSession
from qal.program import protocol, registry, service


def limitations(prog: Experiment, cand: dict[str, Any], proto: dict[str, Any], ev: dict[str, Any]) -> list[str]:
    out = []
    mode = prog.config["mode"]
    if mode == "A":
        out.append("Historical replay: not prospective evidence. " + ((proto.get("replay") or {}).get("note") or ""))
    if mode == "C" or cand["is_synthetic"]:
        out.append("Synthetic data: the result tests the procedure, not any market.")
    if mode == "B":
        n = sum(1 for x in prog.results.get("feed_log", []) if x.get("prospective"))
        out.append(f"Prospective bars: {n}. Bars processed long after they became available were not handled in "
                   "real time (see the feed log).")
    if cand["admission"]["kind"] == "exploratory":
        out.append("Exploratory admission: the candidate did not meet the evidence requirements; this result is not "
                   "validation evidence.")
    if any("estimate" in (c.get("source") or "") for c in cand["costs"].values()):
        out.append("Costs are configured estimates (contract or asset-class defaults), not observed costs.")
    if (ev["tracking"] or {}).get("provisional"):
        out.append("Provisional: " + "; ".join(ev["tracking"].get("provisional_reasons", [])))
    out.append("Simulated fills (next bar, cost model, participation cap): queue position, real spreads and market "
               "impact are not observed.")
    out.append("A classification describes this sample under the protocol; it is not a probability of future "
               "profit and it authorises nothing.")
    return out


def build(s: Session, prog: Experiment) -> dict[str, Any]:
    ev = service.refresh_state(s, prog)
    ps = s.get(PaperSession, prog.config["session_id"])
    cand_rec = s.get(Experiment, prog.config["candidate_id"])
    proto_rec = s.get(Experiment, prog.config["protocol_id"])
    cand, proto = cand_rec.config, proto_rec.config
    r = prog.results
    return {
        "program": {**service.summary_row(prog), "config": prog.config, "config_hash": prog.config_hash,
                    "code_version": prog.code_version, "algorithm_version": prog.algorithm_version},
        "label": protocol.MODES[prog.config["mode"]], "label_note": protocol.MODE_NOTES[prog.config["mode"]],
        "candidate": {**registry.summary(cand_rec), "snapshot": cand},
        "protocol": {"id": proto_rec.id, "hash": proto_rec.config_hash, "integrity_ok": protocol.verify(proto_rec),
                     **proto},
        "session": {"id": ps.id, "status": ps.status, "mode": ps.mode, "bars_processed": ps.bars_processed,
                    "config_hash": ps.config_hash, "is_synthetic": ps.is_synthetic},
        "status": r.get("status"), "rule": ev["rule"], "classification": service.classification(prog),
        "tracking": ev["tracking"], "comparison": ev["comparison"],
        "pauses": r.get("pauses", []), "reviews": r.get("reviews", []), "errors": r.get("errors", []),
        "timeline": r.get("timeline", []), "stopped": r.get("stopped"), "finished": r.get("finished"),
        "feed_log_summary": {"bars": len(r.get("feed_log", [])),
                             "prospective": sum(1 for x in r.get("feed_log", []) if x.get("prospective")),
                             "missing_or_skipped": r.get("missing_bars", 0)},
        "limitations": limitations(prog, cand, proto, ev),
        "simulation_only": "No order was sent to any broker, exchange or account.",
    }
