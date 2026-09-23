from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

CAPS_MINUTES = {"EMER": 24 * 60, "IMP": 14 * 24 * 60}
REFERENCE_PREFIXES = ("hospitalInformation", "practitionerInformation")


def quantiles(values: list[float], points: int = 101) -> list[float]:
    ordered = sorted(values)
    result = []
    for index in range(points):
        position = (len(ordered) - 1) * index / (points - 1)
        lower = math.floor(position)
        upper = math.ceil(position)
        fraction = position - lower
        result.append(round(ordered[lower] + (ordered[upper] - ordered[lower]) * fraction, 1))
    return result


def smoothed_shares(counts: list[int]) -> list[float]:
    smoothed = [count + 1 for count in counts]
    total = sum(smoothed)
    return [round(count / total, 5) for count in smoothed]


def load_encounters(input_dir: Path) -> list[dict]:
    encounters = []
    for path in sorted(input_dir.glob("*.json")):
        if path.name.startswith(REFERENCE_PREFIXES):
            continue
        bundle = json.loads(path.read_text(encoding="utf-8"))
        for entry in bundle.get("entry", []):
            resource = entry.get("resource") or {}
            if resource.get("resourceType") != "Encounter":
                continue
            encounter_class = (resource.get("class") or {}).get("code")
            period = resource.get("period") or {}
            if encounter_class not in CAPS_MINUTES or "start" not in period or "end" not in period:
                continue
            start = datetime.fromisoformat(period["start"])
            end = datetime.fromisoformat(period["end"])
            if end <= start:
                continue
            encounters.append(
                {
                    "patient": (resource.get("subject") or {}).get("reference"),
                    "class": encounter_class,
                    "start": start,
                    "end": end,
                    "minutes": (end - start).total_seconds() / 60,
                }
            )
    return encounters


def admission_rate(encounters: list[dict]) -> tuple[int, int]:
    by_patient: dict[str, list[dict]] = defaultdict(list)
    for encounter in encounters:
        by_patient[encounter["patient"]].append(encounter)

    emergency_total = 0
    admitted = 0
    for items in by_patient.values():
        inpatient_starts = [item["start"] for item in items if item["class"] == "IMP"]
        for item in items:
            if item["class"] != "EMER":
                continue
            emergency_total += 1
            window_end = item["end"] + timedelta(hours=6)
            if any(item["start"] <= start <= window_end for start in inpatient_starts):
                admitted += 1
    return emergency_total, admitted


def main() -> int:
    default_dir = os.getenv("WARDFLOW_SYNTHEA_DIR", str(Path.home() / "pulselake" / "data" / "synthea" / "fhir"))
    parser = argparse.ArgumentParser(description="Calibrate the WardFlow simulator from Synthea FHIR bundles")
    parser.add_argument("--input-dir", type=Path, default=Path(default_dir))
    parser.add_argument("--output", type=Path, default=Path(__file__).with_name("profile.json"))
    args = parser.parse_args()

    if not args.input_dir.is_dir():
        print(f"Input directory not found: {args.input_dir}", file=sys.stderr)
        return 1

    encounters = load_encounters(args.input_dir)
    classes = {}
    for encounter_class, cap in CAPS_MINUTES.items():
        items = [item for item in encounters if item["class"] == encounter_class]
        if len(items) < 10:
            print(f"Not enough {encounter_class} encounters to calibrate ({len(items)})", file=sys.stderr)
            return 1
        hours = [0] * 24
        days = [0] * 7
        for item in items:
            hours[item["start"].hour] += 1
            days[item["start"].weekday()] += 1
        minutes = [min(item["minutes"], cap) for item in items]
        classes[encounter_class] = {
            "encounters": len(items),
            "hour_of_day": smoothed_shares(hours),
            "day_of_week": smoothed_shares(days),
            "los_minutes_median": round(statistics.median(minutes), 1),
            "los_minutes_quantiles": quantiles(minutes),
        }

    emergency_total, admitted = admission_rate(encounters)
    observed = admitted / emergency_total if emergency_total else 0.0
    profile = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": {
            "generator": "Synthea FHIR R4",
            "patients_with_hospital_encounters": len({item["patient"] for item in encounters}),
            "encounters_used": len(encounters),
        },
        "ed_admission_rate": {
            "observed": round(observed, 4),
            "used": round(min(max(observed, 0.08), 0.35), 4),
        },
        "classes": classes,
    }
    args.output.write_text(json.dumps(profile, indent=2) + "\n", encoding="utf-8")

    for encounter_class, data in classes.items():
        print(f"{encounter_class}: {data['encounters']} encounters, median length of stay {data['los_minutes_median'] / 60:.1f} h")
    print(f"ED admission rate: observed {observed:.1%}, used {profile['ed_admission_rate']['used']:.1%}")
    print(f"Profile written to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
