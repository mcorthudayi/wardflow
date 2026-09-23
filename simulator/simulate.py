from __future__ import annotations

import argparse
import heapq
import itertools
import json
import math
import os
import random
import signal
import sys
import time
import uuid
from collections import Counter, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

WARDS = {
    "IM": {"name": "Internal Medicine", "beds": 40, "weight": 0.45},
    "SUR": {"name": "Surgery", "beds": 30, "weight": 0.25},
    "CAR": {"name": "Cardiology", "beds": 20, "weight": 0.18},
    "ICU": {"name": "Intensive Care", "beds": 12, "weight": 0.12},
}
TOTAL_BEDS = sum(ward["beds"] for ward in WARDS.values())
ACUITY_WEIGHTS = [0.02, 0.15, 0.45, 0.30, 0.08]


@dataclass
class Visit:
    visit_id: str
    patient_key: str
    unit: str
    acuity: int | None = None
    bed: str | None = None
    decided_at: datetime | None = None


class Profile:
    def __init__(self, path: Path, min_admission_rate: float) -> None:
        self.data = json.loads(path.read_text(encoding="utf-8"))
        self.observed_admission_rate = self.data["ed_admission_rate"]["observed"]
        self.admission_rate = max(self.data["ed_admission_rate"]["used"], min_admission_rate)

    def length_of_stay(self, encounter_class: str, rng: random.Random) -> timedelta:
        points = self.data["classes"][encounter_class]["los_minutes_quantiles"]
        position = rng.random() * (len(points) - 1)
        lower = int(position)
        upper = min(lower + 1, len(points) - 1)
        minutes = points[lower] + (points[upper] - points[lower]) * (position - lower)
        return timedelta(minutes=max(5.0, minutes))

    def length_biased_stay(self, encounter_class: str, rng: random.Random) -> timedelta:
        ceiling = self.data["classes"][encounter_class]["los_minutes_quantiles"][-1]
        while True:
            stay = self.length_of_stay(encounter_class, rng)
            if rng.random() * ceiling <= stay.total_seconds() / 60:
                return stay

    def mean_length_of_stay(self, encounter_class: str) -> timedelta:
        points = self.data["classes"][encounter_class]["los_minutes_quantiles"]
        average = sum((points[i] + points[i + 1]) / 2 for i in range(len(points) - 1)) / (len(points) - 1)
        return timedelta(minutes=average)

    def hourly_share(self, encounter_class: str, moment: datetime) -> float:
        data = self.data["classes"][encounter_class]
        return data["hour_of_day"][moment.hour] * data["day_of_week"][moment.weekday()] * 7


def poisson(rng: random.Random, lam: float) -> int:
    if lam <= 0:
        return 0
    if lam > 30:
        return max(0, round(rng.gauss(lam, math.sqrt(lam))))
    limit = math.exp(-lam)
    count = 0
    product = rng.random()
    while product > limit:
        count += 1
        product *= rng.random()
    return count


class Simulator:
    def __init__(
        self,
        profile: Profile,
        rng: random.Random,
        start: datetime,
        ed_per_day: float,
        direct_per_day: float,
        warm_occupancy: float,
        emit: Callable[[dict], None],
    ) -> None:
        self.profile = profile
        self.rng = rng
        self.start = start
        self.clock = start
        self.ed_per_day = ed_per_day
        self.direct_per_day = direct_per_day
        self.warm_occupancy = warm_occupancy
        self.emit = emit
        self.queue: list = []
        self.sequence = itertools.count()
        self.free_beds = {
            code: [f"{code}-{number:02d}" for number in range(ward["beds"], 0, -1)] for code, ward in WARDS.items()
        }
        self.waiting: dict[str, deque[Visit]] = {code: deque() for code in WARDS}
        self.ed_census = 0
        self.counts: Counter = Counter()

    def new_id(self, prefix: str) -> str:
        return f"{prefix}_{self.rng.getrandbits(64):016x}"

    def acuity(self) -> int:
        return self.rng.choices([1, 2, 3, 4, 5], weights=ACUITY_WEIGHTS)[0]

    def choose_ward(self) -> str:
        return self.rng.choices(list(WARDS), weights=[ward["weight"] for ward in WARDS.values()])[0]

    def occupied_beds(self) -> int:
        return TOTAL_BEDS - sum(len(beds) for beds in self.free_beds.values())

    def waiting_for_bed(self) -> int:
        return sum(len(queue) for queue in self.waiting.values())

    def schedule(self, at: datetime, kind: str, payload: Visit | None = None) -> None:
        heapq.heappush(self.queue, (at, next(self.sequence), kind, payload))

    def publish(self, event_type: str, event_name: str, at: datetime, visit: Visit, **extra) -> None:
        record = {
            "schema_version": 1,
            "event_id": str(uuid.UUID(int=self.rng.getrandbits(128), version=4)),
            "event_type": event_type,
            "event_name": event_name,
            "occurred_at": at.isoformat(timespec="seconds"),
            "visit_id": visit.visit_id,
            "patient_key": visit.patient_key,
            "unit": visit.unit,
        }
        record.update({key: value for key, value in extra.items() if value is not None})
        self.counts[event_type] += 1
        self.emit(record)

    def warm_start(self, ed_patients: int) -> None:
        for code, ward in WARDS.items():
            for _ in range(min(ward["beds"], round(ward["beds"] * self.warm_occupancy))):
                visit = Visit(self.new_id("v"), self.new_id("p"), code)
                stay = self.profile.length_biased_stay("IMP", self.rng)
                elapsed = stay * self.rng.random()
                visit.bed = self.free_beds[code].pop()
                self.publish("A01", "admit", self.start - elapsed, visit, bed=visit.bed, source="census")
                self.schedule(self.start + (stay - elapsed), "inpatient_discharge", visit)
        for _ in range(ed_patients):
            visit = Visit(self.new_id("v"), self.new_id("p"), "ED", acuity=self.acuity())
            stay = self.profile.length_biased_stay("EMER", self.rng)
            elapsed = stay * self.rng.random()
            self.ed_census += 1
            self.publish("A04", "register", self.start - elapsed, visit, acuity=visit.acuity, source="census")
            self.schedule(self.start + (stay - elapsed), "ed_disposition", visit)

    def on_hour(self, at: datetime, _: Visit | None) -> None:
        for _ in range(poisson(self.rng, self.ed_per_day * self.profile.hourly_share("EMER", at))):
            self.schedule(at + timedelta(seconds=self.rng.uniform(0, 3600)), "ed_arrival")
        for _ in range(poisson(self.rng, self.direct_per_day * self.profile.hourly_share("IMP", at))):
            self.schedule(at + timedelta(seconds=self.rng.uniform(0, 3600)), "direct_admission")
        self.schedule(at + timedelta(hours=1), "hour")

    def on_ed_arrival(self, at: datetime, _: Visit | None) -> None:
        visit = Visit(self.new_id("v"), self.new_id("p"), "ED", acuity=self.acuity())
        self.ed_census += 1
        self.publish("A04", "register", at, visit, acuity=visit.acuity)
        self.schedule(at + self.profile.length_of_stay("EMER", self.rng), "ed_disposition", visit)

    def on_ed_disposition(self, at: datetime, visit: Visit | None) -> None:
        assert visit is not None
        if self.rng.random() < self.profile.admission_rate:
            ward = self.choose_ward()
            visit.decided_at = at
            self.publish("A08", "decision_to_admit", at, visit, target_unit=ward, acuity=visit.acuity)
            self.request_bed(at, visit, ward)
        else:
            self.ed_census -= 1
            self.publish("A03", "discharge", at, visit, acuity=visit.acuity)

    def on_direct_admission(self, at: datetime, _: Visit | None) -> None:
        visit = Visit(self.new_id("v"), self.new_id("p"), "ADMITTING")
        visit.decided_at = at
        self.request_bed(at, visit, self.choose_ward())

    def request_bed(self, at: datetime, visit: Visit, ward: str) -> None:
        if self.free_beds[ward]:
            self.place(at, visit, ward)
        else:
            self.waiting[ward].append(visit)

    def place(self, at: datetime, visit: Visit, ward: str) -> None:
        visit.bed = self.free_beds[ward].pop()
        wait = round((at - visit.decided_at).total_seconds() / 60, 1) if visit.decided_at else None
        if visit.unit == "ED":
            self.ed_census -= 1
            visit.unit = ward
            self.publish("A02", "transfer", at, visit, from_unit="ED", bed=visit.bed, boarding_minutes=wait)
        else:
            visit.unit = ward
            self.publish("A01", "admit", at, visit, bed=visit.bed, wait_minutes=wait)
        self.schedule(at + self.profile.length_of_stay("IMP", self.rng), "inpatient_discharge", visit)

    def on_inpatient_discharge(self, at: datetime, visit: Visit | None) -> None:
        assert visit is not None
        ward = visit.unit
        self.publish("A03", "discharge", at, visit, bed=visit.bed)
        self.free_beds[ward].append(visit.bed)
        if self.waiting[ward]:
            self.place(at, self.waiting[ward].popleft(), ward)

    def run(
        self,
        speed: float,
        until: datetime | None,
        should_stop: Callable[[], bool],
        on_progress: Callable[["Simulator"], None],
    ) -> None:
        handlers = {
            "hour": self.on_hour,
            "ed_arrival": self.on_ed_arrival,
            "ed_disposition": self.on_ed_disposition,
            "direct_admission": self.on_direct_admission,
            "inpatient_discharge": self.on_inpatient_discharge,
        }
        self.schedule(self.start, "hour")
        real_start = time.monotonic()
        next_report = self.start + timedelta(hours=6)

        while self.queue and not should_stop():
            at, _, kind, payload = self.queue[0]
            if until is not None and at > until:
                break
            if speed > 0:
                target = real_start + (at - self.start).total_seconds() / speed
                while not should_stop():
                    remaining = target - time.monotonic()
                    if remaining <= 0:
                        break
                    time.sleep(min(remaining, 0.25))
                if should_stop():
                    break
            heapq.heappop(self.queue)
            self.clock = at
            handlers[kind](at, payload)
            if at >= next_report:
                on_progress(self)
                next_report += timedelta(hours=6)


class StdoutSink:
    def __call__(self, record: dict) -> None:
        sys.stdout.write(json.dumps(record, separators=(",", ":")) + "\n")

    def close(self) -> int:
        sys.stdout.flush()
        return 0


class KafkaSink:
    def __init__(self, brokers: str, topic: str) -> None:
        from confluent_kafka import KafkaException, Producer

        self.topic = topic
        self.delivered = 0
        self.failed = 0
        self.producer = Producer(
            {
                "bootstrap.servers": brokers,
                "client.id": "wardflow-simulator",
                "enable.idempotence": True,
                "acks": "all",
                "linger.ms": 20,
                "compression.type": "lz4",
                "message.timeout.ms": 30000,
            }
        )
        try:
            self.producer.list_topics(topic, timeout=10)
        except KafkaException as error:
            raise SystemExit(f"Cannot reach Kafka at {brokers}: {error}. Is Redpanda running?")

    def _on_delivery(self, error, message) -> None:
        if error is not None:
            self.failed += 1
            print(f"Delivery failed: {error}", file=sys.stderr)
        else:
            self.delivered += 1

    def __call__(self, record: dict) -> None:
        payload = json.dumps(record, separators=(",", ":")).encode("utf-8")
        while True:
            try:
                self.producer.produce(
                    self.topic,
                    key=record["visit_id"].encode("utf-8"),
                    value=payload,
                    on_delivery=self._on_delivery,
                )
                break
            except BufferError:
                self.producer.poll(0.5)
        self.producer.poll(0)

    def close(self) -> int:
        return self.producer.flush(15)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="WardFlow ADT event simulator")
    parser.add_argument("--profile", type=Path, default=Path(__file__).with_name("profile.json"))
    parser.add_argument("--brokers", default=os.getenv("WARDFLOW_BROKERS", "localhost:19092"))
    parser.add_argument("--topic", default=os.getenv("WARDFLOW_TOPIC", "adt.events"))
    parser.add_argument("--speed", type=float, default=600.0)
    parser.add_argument("--sim-hours", type=float, default=0.0)
    parser.add_argument("--ed-per-day", type=float, default=90.0)
    parser.add_argument("--target-occupancy", type=float, default=0.85)
    parser.add_argument("--min-admission-rate", type=float, default=0.15)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-warm-start", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    target_occupancy = min(max(args.target_occupancy, 0.05), 1.0)
    profile = Profile(args.profile, args.min_admission_rate)
    rng = random.Random(args.seed)
    start = datetime.now(timezone.utc).replace(microsecond=0)

    inpatient_days = profile.mean_length_of_stay("IMP").total_seconds() / 86400
    ed_hours = profile.mean_length_of_stay("EMER").total_seconds() / 3600
    ed_admissions = args.ed_per_day * profile.admission_rate
    needed_admissions = target_occupancy * TOTAL_BEDS / inpatient_days
    direct_per_day = max(0.0, needed_admissions - ed_admissions)
    expected_occupancy = (ed_admissions + direct_per_day) * inpatient_days / TOTAL_BEDS
    ed_warm_patients = round(args.ed_per_day * ed_hours / 24)

    sink = StdoutSink() if args.dry_run else KafkaSink(args.brokers, args.topic)
    stop = {"requested": False}

    def request_stop(signum, frame) -> None:
        stop["requested"] = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    simulator = Simulator(profile, rng, start, args.ed_per_day, direct_per_day, target_occupancy, sink)
    last_reported: dict[str, datetime | None] = {"clock": None}

    def report(sim: Simulator) -> None:
        if last_reported["clock"] == sim.clock:
            return
        last_reported["clock"] = sim.clock
        occupied = sim.occupied_beds()
        print(
            f"[{sim.clock:%Y-%m-%d %H:%M}] ED census {sim.ed_census:3d} | "
            f"beds {occupied}/{TOTAL_BEDS} ({occupied / TOTAL_BEDS:.0%}) | "
            f"waiting for bed {sim.waiting_for_bed():2d} | events {sum(sim.counts.values())}",
            file=sys.stderr,
        )

    target = "stdout" if args.dry_run else f"{args.brokers}/{args.topic}"
    print(f"WardFlow simulator -> {target} | speed x{args.speed:g}", file=sys.stderr)
    print(
        f"ED {args.ed_per_day:g}/day (mean stay {ed_hours:.1f} h) | "
        f"admission rate {profile.admission_rate:.0%} (observed {profile.observed_admission_rate:.1%}) | "
        f"direct admissions {direct_per_day:.1f}/day | inpatient mean stay {inpatient_days:.1f} d | "
        f"expected occupancy {expected_occupancy:.0%}",
        file=sys.stderr,
    )

    if not args.no_warm_start:
        simulator.warm_start(ed_warm_patients)
    until = start + timedelta(hours=args.sim_hours) if args.sim_hours > 0 else None

    try:
        simulator.run(args.speed, until, lambda: stop["requested"], report)
    finally:
        undelivered = sink.close()

    report(simulator)
    print("Events by type: " + ", ".join(f"{key}={value}" for key, value in sorted(simulator.counts.items())), file=sys.stderr)
    if undelivered:
        print(f"{undelivered} events were not delivered", file=sys.stderr)
        return 1
    if isinstance(sink, KafkaSink) and sink.failed:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
