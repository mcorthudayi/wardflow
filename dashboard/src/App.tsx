import {
  Area,
  AreaChart,
  Bar,
  CartesianGrid,
  ComposedChart,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis
} from 'recharts'
import { useOps, type LinkState } from './useOps'
import type { Alert, BoardingPatient, Forecast, RecentEvent, UnitStatus } from './types'

type Tone = 'ok' | 'warn' | 'crit'

const EVENT_LABELS: Record<string, string> = {
  A04: 'ED registration',
  A08: 'Decision to admit',
  A02: 'Transfer to ward',
  A01: 'Direct admission',
  A03: 'Discharge'
}

const LINK_LABELS: Record<LinkState, string> = {
  connecting: 'Connecting…',
  live: 'Live',
  reconnecting: 'Reconnecting…',
  offline: 'Offline'
}

const TOOLTIP_STYLE = { background: '#0f172a', border: '1px solid #1e293b', borderRadius: 8, color: '#e2e8f0' }

const clock = (iso: string) => new Date(iso).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' })
const stamp = (iso: string) =>
  new Date(iso).toLocaleString('en-GB', { weekday: 'short', day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' })
const percent = (value: number) => `${Math.round(value * 100)}%`
const toneOf = (ratio: number, warn: number, crit: number): Tone => (ratio >= crit ? 'crit' : ratio >= warn ? 'warn' : 'ok')
const duration = (minutes: number) => {
  const total = Math.max(0, Math.round(minutes))
  return total >= 60 ? `${Math.floor(total / 60)}h ${total % 60}m` : `${total}m`
}
const shortId = (id: string) => id.replace(/^v_/, '').slice(0, 8)

export default function App() {
  const { overview, series, link, error } = useOps()
  const latest = overview?.latest ?? null
  const ed = overview?.units.find(unit => unit.kind === 'emergency')
  const wards = overview?.units.filter(unit => unit.kind === 'ward') ?? []
  const chartData = series.map(snapshot => ({
    time: clock(snapshot.capturedAt),
    occupancy: Math.round(snapshot.occupancy * 1000) / 10,
    edCensus: snapshot.edCensus,
    boarding: snapshot.edBoarding,
    arrivals: snapshot.arrivalsLastHour
  }))

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="logo">◆</span> WardFlow <span className="subtitle">Hospital operations</span>
        </div>
        <div className="clock">
          {overview?.simTime ? stamp(overview.simTime) : '—'}
          <span className="muted"> · simulated time</span>
        </div>
        <div className={`link ${link}`}>
          <span className="dot" />
          {LINK_LABELS[link]}
        </div>
      </header>

      {error && <p className="error">{error}</p>}

      <main className="content">
        <section className="kpis">
          <Kpi
            label="ED census"
            value={latest ? String(latest.edCensus) : '—'}
            hint={ed ? `of ${ed.capacity} treatment spaces` : ''}
            tone={ed && latest ? toneOf(latest.edCensus / ed.capacity, 0.8, 1) : 'ok'}
          />
          <Kpi
            label="Boarding in ED"
            value={latest ? String(latest.edBoarding) : '—'}
            hint="admitted, waiting for a bed"
            tone={latest ? (latest.edBoarding >= 8 ? 'crit' : latest.edBoarding >= 4 ? 'warn' : 'ok') : 'ok'}
          />
          <Kpi
            label="Bed occupancy"
            value={latest ? percent(latest.occupancy) : '—'}
            hint={latest ? `${latest.bedsOccupied} / ${latest.bedsTotal} beds` : ''}
            tone={latest ? toneOf(latest.occupancy, 0.85, 0.95) : 'ok'}
          />
          <Kpi
            label="ED arrivals"
            value={latest ? String(latest.arrivalsLastHour) : '—'}
            hint="last hour"
            tone="ok"
          />
          <Kpi
            label="Avg boarding"
            value={latest?.avgBoardingMinutes != null ? duration(latest.avgBoardingMinutes) : '—'}
            hint="last 24 hours"
            tone={
              latest?.avgBoardingMinutes != null
                ? latest.avgBoardingMinutes >= 240
                  ? 'crit'
                  : latest.avgBoardingMinutes >= 120
                    ? 'warn'
                    : 'ok'
                : 'ok'
            }
          />
          <Kpi
            label="Events processed"
            value={overview ? overview.eventsProcessed.toLocaleString('en-GB') : '—'}
            hint={overview ? `${overview.deadLetters} rejected` : ''}
            tone={overview && overview.deadLetters > 0 ? 'warn' : 'ok'}
          />
        </section>

        <section className="grid">
          <div className="card span-2">
            <h2>Units</h2>
            <div className="units">
              {[...(ed ? [ed] : []), ...wards].map(unit => (
                <UnitCard key={unit.code} unit={unit} />
              ))}
            </div>
          </div>

          <div className="card">
            <h2>Alerts</h2>
            <AlertList alerts={overview?.alerts ?? []} />
          </div>

          <div className="card span-2">
            <h2>Bed occupancy · last 24 h</h2>
            <ResponsiveContainer width="100%" height={230}>
              <AreaChart data={chartData} margin={{ top: 8, right: 8, left: -12, bottom: 0 }}>
                <defs>
                  <linearGradient id="occupancyFill" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="#38bdf8" stopOpacity={0.45} />
                    <stop offset="100%" stopColor="#38bdf8" stopOpacity={0} />
                  </linearGradient>
                </defs>
                <CartesianGrid stroke="#1e293b" vertical={false} />
                <XAxis dataKey="time" stroke="#64748b" fontSize={12} minTickGap={40} />
                <YAxis domain={['dataMin - 5', 100]} stroke="#64748b" fontSize={12} unit="%" allowDecimals={false} />
                <Tooltip contentStyle={TOOLTIP_STYLE} />
                <Area
                  type="monotone"
                  dataKey="occupancy"
                  name="Occupancy %"
                  stroke="#38bdf8"
                  strokeWidth={2}
                  fill="url(#occupancyFill)"
                  isAnimationActive={false}
                />
              </AreaChart>
            </ResponsiveContainer>
          </div>

          <div className="card">
            <h2>Boarding in ED</h2>
            <BoardingTable patients={overview?.boarding ?? []} />
          </div>

          <div className="card span-2">
            <h2>Emergency department · last 24 h</h2>
            <ResponsiveContainer width="100%" height={230}>
              <LineChart data={chartData} margin={{ top: 8, right: 8, left: -12, bottom: 0 }}>
                <CartesianGrid stroke="#1e293b" vertical={false} />
                <XAxis dataKey="time" stroke="#64748b" fontSize={12} minTickGap={40} />
                <YAxis stroke="#64748b" fontSize={12} allowDecimals={false} />
                <Tooltip contentStyle={TOOLTIP_STYLE} />
                <Legend wrapperStyle={{ fontSize: 12 }} />
                <Line type="monotone" dataKey="edCensus" name="ED census" stroke="#a78bfa" strokeWidth={2} dot={false} isAnimationActive={false} />
                <Line type="monotone" dataKey="boarding" name="Boarding" stroke="#f97316" strokeWidth={2} dot={false} isAnimationActive={false} />
                <Line type="monotone" dataKey="arrivals" name="Arrivals / h" stroke="#22c55e" strokeWidth={2} dot={false} isAnimationActive={false} />
              </LineChart>
            </ResponsiveContainer>
          </div>

          <div className="card">
            <h2>Live event stream</h2>
            <EventFeed events={overview?.recentEvents ?? []} />
          </div>

          <div className="card" style={{ gridColumn: '1 / -1' }}>
            <h2>ED arrivals · actual vs forecast</h2>
            <ForecastChart forecast={overview?.forecast ?? null} />
          </div>
        </section>
      </main>

      <footer className="footer">
        Synthetic data calibrated on Synthea encounters · HL7 ADT events via Redpanda · pseudonymous identifiers only
      </footer>
    </div>
  )
}

function Kpi({ label, value, hint, tone }: { label: string; value: string; hint: string; tone: Tone }) {
  return (
    <div className={`kpi ${tone}`}>
      <span className="kpi-label">{label}</span>
      <strong className="kpi-value">{value}</strong>
      <span className="kpi-hint">{hint}</span>
    </div>
  )
}

function UnitCard({ unit }: { unit: UnitStatus }) {
  const ratio = unit.capacity ? unit.occupied / unit.capacity : 0
  const tone = unit.kind === 'emergency' ? toneOf(ratio, 0.8, 1) : toneOf(ratio, 0.85, 0.95)
  const footer =
    unit.kind === 'emergency'
      ? 'patients in department'
      : unit.boardingForUnit > 0
        ? `${unit.boardingForUnit} waiting in ED`
        : 'no one waiting'

  return (
    <div className={`unit ${tone}`}>
      <div className="unit-head">
        <strong>{unit.code}</strong>
        <span className="muted">{unit.name}</span>
      </div>
      <div className="unit-value">
        {unit.occupied}
        <span className="muted"> / {unit.capacity}</span>
      </div>
      <div className="meter">
        <div className="meter-fill" style={{ width: `${Math.min(100, ratio * 100)}%` }} />
      </div>
      <div className="unit-foot muted">{footer}</div>
    </div>
  )
}

function AlertList({ alerts }: { alerts: Alert[] }) {
  if (alerts.length === 0) return <p className="calm">All units within normal limits.</p>
  return (
    <ul className="alerts">
      {alerts.map((alert, index) => (
        <li key={`${alert.title}-${index}`} className={`alert ${alert.severity}`}>
          <strong>{alert.title}</strong>
          <span>{alert.detail}</span>
        </li>
      ))}
    </ul>
  )
}

function BoardingTable({ patients }: { patients: BoardingPatient[] }) {
  if (patients.length === 0) return <p className="calm">No patients waiting for a bed.</p>
  return (
    <table>
      <thead>
        <tr>
          <th>Visit</th>
          <th>Ward</th>
          <th>ESI</th>
          <th>Waiting</th>
        </tr>
      </thead>
      <tbody>
        {patients.map(patient => (
          <tr key={patient.visitId}>
            <td>
              <code>{shortId(patient.visitId)}</code>
            </td>
            <td>{patient.targetUnit ?? '—'}</td>
            <td>{patient.acuity ?? '—'}</td>
            <td className={patient.waitingMinutes >= 240 ? 'crit-text' : patient.waitingMinutes >= 120 ? 'warn-text' : ''}>
              {duration(patient.waitingMinutes)}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function EventFeed({ events }: { events: RecentEvent[] }) {
  if (events.length === 0) return <p className="calm">Waiting for events…</p>
  return (
    <ul className="feed">
      {events.map((event, index) => (
        <li key={`${event.visitId}-${event.eventType}-${index}`}>
          <span className={`tag ${event.eventType}`}>{event.eventType}</span>
          <span className="feed-text">{EVENT_LABELS[event.eventType] ?? event.eventType}</span>
          <span className="muted">
            {clock(event.occurredAt)} · <code>{shortId(event.visitId)}</code> · {event.currentUnit}
          </span>
        </li>
      ))}
    </ul>
  )
}

function ForecastChart({ forecast }: { forecast: Forecast | null }) {
  if (!forecast || !forecast.ready) {
    return (
      <p className="muted">
        The forecast warms up after 3 hours of simulated arrivals
        {forecast ? ` (${forecast.historyHours} h so far)` : ''}.
      </p>
    )
  }

  const data = [
    ...forecast.history.map(point => ({
      time: clock(point.hourStart),
      actual: point.actual,
      fitted: point.expected,
      range: [point.low, point.high]
    })),
    ...forecast.next.map(point => ({
      time: clock(point.hourStart),
      forecast: point.expected,
      range: [point.low, point.high]
    }))
  ]

  return (
    <>
      <p className="muted" style={{ margin: '0 0 12px', fontSize: 13 }}>
        Seasonal Poisson model: hour-of-day and weekday shape calibrated on Synthea, level re-estimated from the last{' '}
        {forecast.historyHours} h ({forecast.dailyRate} arrivals/day). Mean absolute error over the last 12 h:{' '}
        {forecast.mae ?? '—'} arrivals/h. Shaded band: 80% interval.
      </p>
      <ResponsiveContainer width="100%" height={240}>
        <ComposedChart data={data} margin={{ top: 8, right: 8, left: -12, bottom: 0 }}>
          <CartesianGrid stroke="#1e293b" vertical={false} />
          <XAxis dataKey="time" stroke="#64748b" fontSize={12} />
          <YAxis stroke="#64748b" fontSize={12} allowDecimals={false} />
          <Tooltip contentStyle={TOOLTIP_STYLE} />
          <Legend wrapperStyle={{ fontSize: 12 }} />
          <Area type="monotone" dataKey="range" name="80% interval" stroke="none" fill="#38bdf8" fillOpacity={0.15} isAnimationActive={false} />
          <Bar dataKey="actual" name="Actual arrivals" fill="#22c55e" barSize={18} isAnimationActive={false} />
          <Line type="monotone" dataKey="fitted" name="Model fit" stroke="#94a3b8" strokeDasharray="4 4" dot={false} isAnimationActive={false} />
          <Line type="monotone" dataKey="forecast" name="Forecast" stroke="#f59e0b" strokeWidth={2} dot={{ r: 3 }} isAnimationActive={false} />
        </ComposedChart>
      </ResponsiveContainer>
    </>
  )
}
