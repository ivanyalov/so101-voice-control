"""Return both team arms to the recorded pre-test pose, within calibrated limits."""

import argparse
from datetime import datetime, timezone
import json

from robot_pair_test import PairTest
from robot_probe import ROOT, JOINTS

INITIAL_RECORD = ROOT / ".cache/robot/20261103T074229990706Z_pair-test.json"
MAX_RETURN_TICKS = 150  # About 13 degrees; prevents a large unreviewed home move.
MAX_BASE_RETURN_TICKS = 683  # Up to 60 degrees for returning repeated base jogs.


def return_plan(arms, record):
    if record.get("mode") != "execute" or set(arms) != {"leader", "follower"}:
        raise ValueError("Missing the original two-arm execution snapshot")
    original = record["snapshot"]
    moves = []
    for joint in JOINTS:
        for role in ("follower", "leader"):
            current, saved = arms[role][joint], original[role][joint]
            cal = current["calibration"]
            if cal != saved["calibration"]:
                raise ValueError(f"Calibration changed for {role}/{joint}")
            start = current["state"]["present_position"]
            recorded = saved["state"]["present_position"]
            target = min(cal["range_max"], max(cal["range_min"], recorded))
            maximum = MAX_BASE_RETURN_TICKS if joint == "shoulder_pan" else MAX_RETURN_TICKS
            if abs(target - start) > maximum:
                raise ValueError(f"{role}/{joint} is too far from the saved pose; no motors were moved")
            moves.append({"role": role, "joint": joint, "id": cal["id"],
                          "start": start, "target": target, "recorded_initial": recorded,
                          "adjusted_to_calibrated_limit": recorded != target,
                          "range_min": cal["range_min"], "range_max": cal["range_max"]})
    return moves


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Move both arms; otherwise only read and print targets")
    args = parser.parse_args()
    record = json.loads(INITIAL_RECORD.read_text())
    test = PairTest()
    report = {"timestamp_utc": datetime.now(timezone.utc).isoformat(), "source_record": str(INITIAL_RECORD),
              "mode": "execute" if args.execute else "read_only_plan", "eeprom_writes": 0}
    code = 0
    try:
        report["snapshot"] = test.open_and_read(require_torque_off=False)
        report["moves"] = return_plan(report["snapshot"], record)
        print("Both arms: recorded pre-test pose, restricted to calibrated limits.", flush=True)
        if args.execute:
            print("Returning slowly. Ctrl+C requests holding the measured positions.", flush=True)
            report["result"] = test.execute(report["moves"])
            report["status"] = "returned_holding"
            print("Both arms reached the return targets; motors remain energized and holding.", flush=True)
        else:
            report["status"] = "planned_no_motion"
            for m in report["moves"]:
                print(f"{m['role']:8} {m['joint']:14}: {m['start']} -> {m['target']}", flush=True)
    except (Exception, KeyboardInterrupt) as exc:
        report.update(status="return_incomplete", error=str(exc) or "KeyboardInterrupt")
        report["hold_errors"] = test.hold()
        report["motors_may_be_energized"] = bool(test.attempted)
        print("Return incomplete: " + report["error"], flush=True)
        if test.samples:
            for m in test.samples[-1]["motors"]:
                if abs(m["target_error_ticks"]) > 12:
                    print(f"  {m['role']}/{m['joint']}: target error {m['target_error_ticks']} ticks", flush=True)
        if report["hold_errors"]:
            print("Could not confirm holding: " + "; ".join(report["hold_errors"]), flush=True)
        code = 1
    finally:
        test.close()
        report.update(register_writes=test.writes, samples=test.samples)
        output = ROOT / ".cache/robot" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "_return.json")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2) + "\n")
        print(f"Report: {output}", flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
