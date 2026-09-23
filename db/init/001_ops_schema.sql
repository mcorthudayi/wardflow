CREATE SCHEMA IF NOT EXISTS ops;

CREATE TABLE IF NOT EXISTS ops.units (
    code     TEXT PRIMARY KEY,
    name     TEXT NOT NULL,
    kind     TEXT NOT NULL CHECK (kind IN ('emergency', 'ward')),
    capacity INT  NOT NULL CHECK (capacity > 0)
);

INSERT INTO ops.units (code, name, kind, capacity) VALUES
    ('ED',  'Emergency Department', 'emergency', 30),
    ('IM',  'Internal Medicine',    'ward',      40),
    ('SUR', 'Surgery',              'ward',      30),
    ('CAR', 'Cardiology',           'ward',      20),
    ('ICU', 'Intensive Care',       'ward',      12)
ON CONFLICT (code) DO UPDATE
SET name = EXCLUDED.name, kind = EXCLUDED.kind, capacity = EXCLUDED.capacity;

CREATE TABLE IF NOT EXISTS ops.beds (
    bed_id      TEXT PRIMARY KEY,
    unit        TEXT NOT NULL REFERENCES ops.units (code),
    occupied_by TEXT,
    assigned_at TIMESTAMPTZ,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE ops.beds ADD COLUMN IF NOT EXISTS assigned_at TIMESTAMPTZ;

INSERT INTO ops.beds (bed_id, unit)
SELECT u.code || '-' || lpad(n::text, 2, '0'), u.code
FROM ops.units u
CROSS JOIN LATERAL generate_series(1, u.capacity) AS n
WHERE u.kind = 'ward'
ON CONFLICT (bed_id) DO NOTHING;

CREATE TABLE IF NOT EXISTS ops.visits (
    visit_id         TEXT PRIMARY KEY,
    patient_key      TEXT NOT NULL,
    status           TEXT NOT NULL CHECK (status IN ('ed', 'boarding', 'inpatient', 'discharged')),
    current_unit     TEXT NOT NULL,
    target_unit      TEXT,
    bed_id           TEXT,
    acuity           INT,
    ed_arrived_at    TIMESTAMPTZ,
    decision_at      TIMESTAMPTZ,
    admitted_at      TIMESTAMPTZ,
    discharged_at    TIMESTAMPTZ,
    boarding_minutes NUMERIC,
    wait_minutes     NUMERIC,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_visits_status ON ops.visits (status);
CREATE INDEX IF NOT EXISTS idx_visits_admitted_at ON ops.visits (admitted_at);
CREATE INDEX IF NOT EXISTS idx_visits_ed_arrived_at ON ops.visits (ed_arrived_at);
CREATE INDEX IF NOT EXISTS idx_visits_decision_at ON ops.visits (decision_at);

CREATE TABLE IF NOT EXISTS ops.processed_events (
    event_id        UUID PRIMARY KEY,
    event_type      TEXT NOT NULL,
    visit_id        TEXT NOT NULL,
    occurred_at     TIMESTAMPTZ NOT NULL,
    kafka_partition INT NOT NULL,
    kafka_offset    BIGINT NOT NULL,
    processed_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_processed_events_type_time ON ops.processed_events (event_type, occurred_at);

CREATE TABLE IF NOT EXISTS ops.dead_letter (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kafka_partition INT NOT NULL,
    kafka_offset    BIGINT NOT NULL,
    message_key     TEXT,
    payload         TEXT,
    error           TEXT NOT NULL,
    received_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (kafka_partition, kafka_offset)
);

CREATE TABLE IF NOT EXISTS ops.kpi_snapshots (
    captured_at          TIMESTAMPTZ PRIMARY KEY,
    ed_census            INT NOT NULL,
    ed_boarding          INT NOT NULL,
    beds_occupied        INT NOT NULL,
    beds_total           INT NOT NULL,
    occupancy            NUMERIC(5, 4) NOT NULL,
    arrivals_last_hour   INT NOT NULL,
    discharges_last_hour INT NOT NULL,
    avg_boarding_minutes NUMERIC,
    unit_occupancy       JSONB NOT NULL
);

CREATE OR REPLACE VIEW ops.v_unit_status AS
SELECT
    u.code,
    u.name,
    u.kind,
    u.capacity,
    CASE
        WHEN u.kind = 'ward' THEN
            (SELECT count(*) FROM ops.beds b WHERE b.unit = u.code AND b.occupied_by IS NOT NULL)
        ELSE
            (SELECT count(*) FROM ops.visits v WHERE v.current_unit = 'ED' AND v.status IN ('ed', 'boarding'))
    END AS occupied,
    (SELECT count(*) FROM ops.visits v WHERE v.status = 'boarding' AND v.target_unit = u.code) AS boarding_for_unit
FROM ops.units u;
