#!/usr/bin/env python3
"""Fail CI if publish/events/unlock.json lacks postprocess markers."""
import json
import sys


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "publish/events/unlock.json"
    with open(path, encoding="utf-8") as f:
        lib = json.load(f)
    pp = lib.get("unlock_postprocess")
    if not pp:
        print(f"::error file={path}::missing unlock_postprocess (merge/postprocess did not run on deploy tree)")
        return 1
    events = lib.get("events") or []
    visible = [e for e in events if not e.get("merged_into")]
    if not visible:
        print(f"::error file={path}::no visible unlock events")
        return 1
    if not any(e.get("impact_score") is not None for e in visible):
        print(f"::error file={path}::no impact_score on any event")
        return 1
    hype = [e for e in visible if (e.get("fields") or {}).get("ticker") == "HYPE"]
    trump = [e for e in visible if (e.get("fields") or {}).get("ticker") == "TRUMP"]
    if not hype or not trump:
        print(f"::error file={path}::manual HYPE/TRUMP missing (hype={len(hype)} trump={len(trump)})")
        return 1
    ena = [
        e for e in visible
        if (e.get("fields") or {}).get("ticker") == "ENA"
        and str(e.get("event_time_utc8", "")).startswith("2026-10-05")
    ]
    if ena:
        amt = (ena[0].get("fields") or {}).get("amount")
        if amt is None or float(amt) < 1e9:
            print(f"::error file={path}::ENA 2026-10-05 amount not corrected: {amt}")
            return 1
    print(json.dumps({
        "updated": lib.get("updated"),
        "unlock_postprocess": pp,
        "total": len(events),
        "visible": len(visible),
        "merged_shadows": sum(1 for e in events if e.get("merged_into")),
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
