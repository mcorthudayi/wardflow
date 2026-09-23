from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import time
import uuid
from collections import Counter
from datetime import datetime, timedelta

import psycopg
from confluent_kafka import Consumer, KafkaError, KafkaException
from dotenv import load_dotenv

log = logging.getLogger("wardflow.processor")

EVENT_TYPES = {"A01", "A02", "A03", "A04", "A08"}
REQUIRED_FIELDS = ("schema_version", "event_id", "event_type", "occurred_at", "visit_id", "patient_key", "unit")
SNAPSHOT_EVERY = timedelta(minutes=15)

DEDUP_SQL = """
INSERT INTO ops.processed_events (event_id, event_type, visit_id, occurred_at, kafka_partition, kafka_offset)
VALUES (%(event_id)s, %(event_type)s, %(visit_id)s, %(occurred_at)s, %(partition)s, %(offset)s)
ON CONFLICT (event_id) DO NOTHING
"""

REGISTER_SQL = """
INSERT INTO ops.visits (visit_id, patient_key, status, current_unit, acuity, ed_arrived_at, updated_at)
VALUES (%(visit_id)s, %(patient_key)s, 'ed', 'ED', %(acuity)s, %(occurred_at)s, now())
ON CONFLICT (visit_id) DO UPDATE
SET status = 'ed', current_unit = 'ED', acuity = EXCLUDED.acuity,
    ed_arrived_at = EXCLUDED.ed_arrived_at, updated_at = now()
"""

DECISION_SQL = """
UPDATE ops.visits
SET status = 'boarding', target_unit = %(target_unit)s, decision_at = %(occurred_at)s, updated_at = now()
WHERE visit_id = %(visit_id)s
"""

ADMIT_SQL = """
INSERT INTO ops.visits
    (visit_id, patient_key, status, current_unit, bed_id, admitted_at, boarding_minutes, wait_minutes, updated_at)
VALUES
    (%(visit_id)s, %(patient_key)s, 'inpatient', %(unit)s, %(bed)s, %(occurred_at)s,
     %(boarding_minutes)s, %(wait_minutes)s, now())
ON CONFLICT (visit_id) DO UPDATE
SET status = 'inpatient', current_unit = EXCLUDED.current_unit, bed_id = EXCLUDED.bed_id,
    admitted_at = EXCLUDED.admitted_at, boarding_minutes = EXCLUDED.boarding_minutes,
    wait_minutes = EXCLUDED.wait_minutes, updated_at = now()
"""

OCCUPY_BED_SQL = """
UPDATE ops.beds
SET occupied_by = %(visit_id)s, assigned_at = %(occurred_at)s, updated_at = now()
WHERE bed_id = %(bed)s AND (assigned_at IS NULL OR assigned_at <= %(occurred_at)s)
"""

BED_EXISTS_SQL = "SELECT 1 FROM ops.beds WHERE bed_id = %(bed)s"

RELEASE_BED_SQL = """
UPDATE ops.beds SET occupied_by = NULL, updated_at = now()
WHERE occupied_by = %(visit_id)s
"""

DISCHARGE_SQL = """
UPDATE ops.visits
SET status = 'discharged', discharged_at = %(occurred_at)s, updated_at = now()
WHERE visit_id = %(visit_id)s
"""

DEAD_LETTER_SQL = """
INSERT INTO ops.dead_letter (kafka_partition, kafka_offset, message_key, payload, error)
VALUES (%(partition)s, %(offset)s, %(key)s, %(payload)s, %(error)s)
ON CONFLICT (kafka_partition, kafka_offset) DO NOTHING
"""

SNAPSHOT_SQL = """
INSERT INTO ops.kpi_snapshots
    (captured_at, ed_census, ed_boarding, beds_occupied, beds_total, occupancy,
     arrivals_last_hour, discharges_last_hour, avg_boarding_minutes, unit_occupancy)
SELECT
    ts,
    (SELECT count(*) FROM ops.visits v
     WHERE v.ed_arrived_at <= ts
       AND coalesce(v.admitted_at, v.discharged_at, 'infinity'::timestamptz) > ts),
    (SELECT count(*) FROM ops.visits v
     WHERE v.decision_at <= ts
       AND coalesce(v.admitted_at, 'infinity'::timestamptz) > ts),
    occupancy.occupied,
    capacity.total,
    round(occupancy.occupied::numeric / greatest(capacity.total, 1), 4),
    (SELECT count(*) FROM ops.processed_events e
     WHERE e.event_type = 'A04' AND e.occurred_at > ts - interval '1 hour' AND e.occurred_at <= ts),
    (SELECT count(*) FROM ops.processed_events e
     WHERE e.event_type = 'A03' AND e.occurred_at > ts - interval '1 hour' AND e.occurred_at <= ts),
    (SELECT round(avg(v.boarding_minutes), 1) FROM ops.visits v
     WHERE v.boarding_minutes IS NOT NULL
       AND v.admitted_at > ts - interval '24 hours'
       AND v.admitted_at <= ts),
    (SELECT coalesce(jsonb_object_agg(u.code, (
         SELECT count(*) FROM ops.visits v
         WHERE v.current_unit = u.code
           AND v.admitted_at <= ts
           AND coalesce(v.discharged_at, 'infinity'::timestamptz) > ts)), '{}'::jsonb)
     FROM ops.units u WHERE u.kind = 'ward')
FROM generate_series(%(start)s::timestamptz, %(end)s::timestamptz, interval '15 minutes') AS ts
CROSS JOIN (SELECT sum(capacity)::int AS total FROM ops.units WHERE kind = 'ward') AS capacity
CROSS JOIN LATERAL (
    SELECT count(*)::int AS occupied FROM ops.visits v
    WHERE v.admitted_at <= ts AND coalesce(v.discharged_at, 'infinity'::timestamptz) > ts
) AS occupancy
ON CONFLICT (captured_at) DO NOTHING
"""


class InvalidEvent(Exception):
    pass


def parse_event(raw: bytes | None) -> dict:
    if raw is None:
        raise InvalidEvent("empty payload")
    try:
        event = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise InvalidEvent(f"invalid JSON: {error}")
    if not isinstance(event, dict):
        raise InvalidEvent("payload is not a JSON object")
    missing = [field for field in REQUIRED_FIELDS if field not in event]
    if missing:
        raise InvalidEvent("missing fields: " + ", ".join(missing))
    if event["schema_version"] != 1:
        raise InvalidEvent(f"unsupported schema_version {event['schema_version']}")
    if event["event_type"] not in EVENT_TYPES:
        raise InvalidEvent(f"unknown event_type {event['event_type']}")
    try:
        event["event_id"] = uuid.UUID(str(event["event_id"]))
        event["occurred_at"] = datetime.fromisoformat(str(event["occurred_at"]))
    except (ValueError, TypeError) as error:
        raise InvalidEvent(f"invalid field value: {error}")
    if event["occurred_at"].tzinfo is None:
        raise InvalidEvent("occurred_at must include a timezone offset")
    if event["event_type"] in {"A01", "A02"} and not event.get("bed"):
        raise InvalidEvent("admission or transfer without a bed")
    return event


def apply_event(cursor: psycopg.Cursor, event: dict) -> bool:
    params = {
        "visit_id": event["visit_id"],
        "patient_key": event["patient_key"],
        "unit": event["unit"],
        "occurred_at": event["occurred_at"],
        "acuity": event.get("acuity"),
        "target_unit": event.get("target_unit"),
        "bed": event.get("bed"),
        "boarding_minutes": event.get("boarding_minutes"),
        "wait_minutes": event.get("wait_minutes"),
    }
    event_type = event["event_type"]
    if event_type == "A04":
        cursor.execute(REGISTER_SQL, params)
    elif event_type == "A08":
        cursor.execute(DECISION_SQL, params)
    elif event_type in {"A01", "A02"}:
        cursor.execute(ADMIT_SQL, params)
        cursor.execute(OCCUPY_BED_SQL, params)
        if cursor.rowcount == 0:
            cursor.execute(BED_EXISTS_SQL, params)
            if cursor.fetchone() is None:
                raise InvalidEvent(f"unknown bed {params['bed']}")
            return True
    elif event_type == "A03":
        cursor.execute(RELEASE_BED_SQL, params)
        cursor.execute(DISCHARGE_SQL, params)
    return False


def dead_letter(conn: psycopg.Connection, message, error: str) -> None:
    key = message.key()
    payload = message.value()
    conn.execute(
        DEAD_LETTER_SQL,
        {
            "partition": message.partition(),
            "offset": message.offset(),
            "key": key.decode("utf-8", "replace") if key else None,
            "payload": payload.decode("utf-8", "replace") if payload else None,
            "error": error[:500],
        },
    )


def process_batch(conn: psycopg.Connection, messages: list, stats: Counter) -> list[tuple[int, datetime]]:
    progress: list[tuple[int, datetime]] = []
    with conn.transaction():
        for message in messages:
            try:
                event = parse_event(message.value())
            except InvalidEvent as error:
                dead_letter(conn, message, str(error))
                stats["dead_lettered"] += 1
                continue

            stale = False
            try:
                with conn.transaction():
                    with conn.cursor() as cursor:
                        cursor.execute(
                            DEDUP_SQL,
                            {
                                "event_id": event["event_id"],
                                "event_type": event["event_type"],
                                "visit_id": event["visit_id"],
                                "occurred_at": event["occurred_at"],
                                "partition": message.partition(),
                                "offset": message.offset(),
                            },
                        )
                        duplicate = cursor.rowcount == 0
                        if not duplicate:
                            stale = apply_event(cursor, event)
            except InvalidEvent as error:
                dead_letter(conn, message, str(error))
                stats["dead_lettered"] += 1
                continue

            if duplicate:
                stats["duplicates"] += 1
            else:
                stats["applied"] += 1
                if stale:
                    stats["stale_bed_updates"] += 1

            if event.get("source") != "census":
                progress.append((message.partition(), event["occurred_at"]))
    return progress


def floor_to_interval(moment: datetime) -> datetime:
    return moment - timedelta(
        minutes=moment.minute % int(SNAPSHOT_EVERY.total_seconds() // 60),
        seconds=moment.second,
        microseconds=moment.microsecond,
    )


class Watermarks:
    def __init__(self) -> None:
        self.per_partition: dict[int, datetime] = {}
        self.first_event: datetime | None = None
        self.max_event: datetime | None = None

    def observe(self, partition: int, occurred_at: datetime) -> None:
        current = self.per_partition.get(partition)
        if current is None or occurred_at > current:
            self.per_partition[partition] = occurred_at
        if self.first_event is None or occurred_at < self.first_event:
            self.first_event = occurred_at
        if self.max_event is None or occurred_at > self.max_event:
            self.max_event = occurred_at

    def low(self, partitions: set[int]) -> datetime | None:
        if not partitions or any(partition not in self.per_partition for partition in partitions):
            return None
        return min(self.per_partition[partition] for partition in partitions)


def resolve_next_snapshot(conn: psycopg.Connection, watermarks: Watermarks) -> datetime | None:
    latest = conn.execute("SELECT max(captured_at) FROM ops.kpi_snapshots").fetchone()[0]
    if latest is not None:
        return latest + SNAPSHOT_EVERY
    if watermarks.first_event is not None:
        return floor_to_interval(watermarks.first_event) + SNAPSHOT_EVERY
    return None


def write_snapshots(conn: psycopg.Connection, start: datetime, end: datetime) -> int:
    if end < start:
        return 0
    return conn.execute(SNAPSHOT_SQL, {"start": start, "end": end}).rowcount


def connect() -> psycopg.Connection:
    return psycopg.connect(
        host=os.getenv("POSTGRES_HOST", "localhost"),
        port=os.getenv("POSTGRES_PORT", "5434"),
        dbname=os.getenv("POSTGRES_DB", "wardflow"),
        user=os.getenv("POSTGRES_USER", "wardflow"),
        password=os.environ["POSTGRES_PASSWORD"],
        autocommit=True,
    )


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="WardFlow stream processor")
    parser.add_argument("--brokers", default=os.getenv("WARDFLOW_BROKERS", "127.0.0.1:19092"))
    parser.add_argument("--topic", default=os.getenv("WARDFLOW_TOPIC", "adt.events"))
    parser.add_argument("--group", default="wardflow-processor")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--idle-seconds", type=float, default=10.0)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if not os.getenv("POSTGRES_PASSWORD"):
        log.error("POSTGRES_PASSWORD is not set. Copy .env.example to .env and set a password.")
        return 1

    stop = {"requested": False}

    def request_stop(signum, frame) -> None:
        stop["requested"] = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    consumer = Consumer(
        {
            "bootstrap.servers": args.brokers,
            "group.id": args.group,
            "client.id": "wardflow-processor",
            "enable.auto.commit": False,
            "auto.offset.reset": "earliest",
        }
    )
    consumer.subscribe([args.topic])

    stats: Counter = Counter()
    watermarks = Watermarks()
    next_snapshot: datetime | None = None
    snapshots = 0
    last_message = time.monotonic()
    last_log = time.monotonic()
    log.info("Consuming %s as group %s (%s)", args.topic, args.group, "drain mode" if args.once else "continuous")

    def report(prefix: str) -> None:
        assigned = {tp.partition for tp in consumer.assignment()}
        low = watermarks.low(assigned)
        log.info(
            "%sapplied=%d duplicates=%d stale_bed_updates=%d dead_lettered=%d snapshots=%d low_watermark=%s",
            prefix, stats["applied"], stats["duplicates"], stats["stale_bed_updates"],
            stats["dead_lettered"], snapshots, low.isoformat(timespec="minutes") if low else "-",
        )

    try:
        with connect() as conn:
            while not stop["requested"]:
                batch = []
                for message in consumer.consume(num_messages=args.batch_size, timeout=1.0):
                    error = message.error()
                    if error is None:
                        batch.append(message)
                    elif error.code() != KafkaError._PARTITION_EOF:
                        raise KafkaException(error)

                if batch:
                    last_message = time.monotonic()
                    for partition, occurred_at in process_batch(conn, batch, stats):
                        watermarks.observe(partition, occurred_at)
                    consumer.commit(asynchronous=False)

                    if next_snapshot is None:
                        next_snapshot = resolve_next_snapshot(conn, watermarks)
                    low = watermarks.low({tp.partition for tp in consumer.assignment()})
                    if next_snapshot is not None and low is not None and low >= next_snapshot:
                        end = floor_to_interval(low)
                        snapshots += write_snapshots(conn, next_snapshot, end)
                        next_snapshot = end + SNAPSHOT_EVERY
                elif args.once and time.monotonic() - last_message >= args.idle_seconds:
                    break

                if time.monotonic() - last_log >= 5:
                    report("")
                    last_log = time.monotonic()

            if args.once and watermarks.max_event is not None:
                if next_snapshot is None:
                    next_snapshot = resolve_next_snapshot(conn, watermarks)
                if next_snapshot is not None and watermarks.max_event >= next_snapshot:
                    end = floor_to_interval(watermarks.max_event)
                    snapshots += write_snapshots(conn, next_snapshot, end)
                    next_snapshot = end + SNAPSHOT_EVERY
    finally:
        report("Done: ")
        consumer.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
