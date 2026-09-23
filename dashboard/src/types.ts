export interface Snapshot {
  capturedAt: string
  edCensus: number
  edBoarding: number
  bedsOccupied: number
  bedsTotal: number
  occupancy: number
  arrivalsLastHour: number
  dischargesLastHour: number
  avgBoardingMinutes: number | null
}

export interface UnitStatus {
  code: string
  name: string
  kind: 'emergency' | 'ward'
  capacity: number
  occupied: number
  boardingForUnit: number
}

export interface Alert {
  severity: 'critical' | 'warning' | 'info'
  title: string
  detail: string
}

export interface BoardingPatient {
  visitId: string
  targetUnit: string | null
  acuity: number | null
  decisionAt: string
  waitingMinutes: number
}

export interface RecentEvent {
  eventType: string
  occurredAt: string
  visitId: string
  currentUnit: string
  status: string
}

export interface Overview {
  simTime: string | null
  latest: Snapshot | null
  units: UnitStatus[]
  alerts: Alert[]
  boarding: BoardingPatient[]
  recentEvents: RecentEvent[]
  deadLetters: number
  eventsProcessed: number
}
