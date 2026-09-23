import { HubConnectionBuilder, LogLevel } from '@microsoft/signalr'
import { useEffect, useRef, useState } from 'react'
import type { Overview, Snapshot } from './types'

export type LinkState = 'connecting' | 'live' | 'reconnecting' | 'offline'

const SNAPSHOT_GAP_MS = 15 * 60 * 1000
const WINDOW_MS = 24 * 60 * 60 * 1000

function mergeSnapshot(series: Snapshot[], snapshot: Snapshot): Snapshot[] {
  if (series.some(item => item.capturedAt === snapshot.capturedAt)) return series
  const cutoff = Date.parse(snapshot.capturedAt) - WINDOW_MS
  return [...series, snapshot]
    .sort((a, b) => Date.parse(a.capturedAt) - Date.parse(b.capturedAt))
    .filter(item => Date.parse(item.capturedAt) > cutoff)
}

export function useOps() {
  const [overview, setOverview] = useState<Overview | null>(null)
  const [series, setSeries] = useState<Snapshot[]>([])
  const [link, setLink] = useState<LinkState>('connecting')
  const [error, setError] = useState<string | null>(null)
  const lastCaptured = useRef<string | null>(null)

  useEffect(() => {
    let disposed = false

    const loadSeries = async () => {
      try {
        const response = await fetch('/api/kpis?hours=24')
        if (!response.ok) throw new Error(`KPI request failed (${response.status})`)
        const data = (await response.json()) as Snapshot[]
        if (disposed) return
        setSeries(data)
        lastCaptured.current = data.length ? data[data.length - 1].capturedAt : null
        setError(null)
      } catch (err) {
        if (!disposed) setError(err instanceof Error ? err.message : 'Failed to load KPI history')
      }
    }

    void loadSeries()

    const connection = new HubConnectionBuilder()
      .withUrl('/hubs/ops')
      .withAutomaticReconnect([0, 1000, 2000, 5000, 10000])
      .configureLogging(LogLevel.Warning)
      .build()

    connection.on('overview', (next: Overview) => {
      if (disposed) return
      setOverview(next)
      const latest = next.latest
      if (!latest) return
      const previous = lastCaptured.current
      if (previous && Date.parse(latest.capturedAt) - Date.parse(previous) > SNAPSHOT_GAP_MS) {
        void loadSeries()
        return
      }
      lastCaptured.current = latest.capturedAt
      setSeries(current => mergeSnapshot(current, latest))
    })

    connection.onreconnecting(() => {
      if (!disposed) setLink('reconnecting')
    })
    connection.onreconnected(() => {
      if (disposed) return
      setLink('live')
      void loadSeries()
    })
    connection.onclose(() => {
      if (!disposed) setLink('offline')
    })

    connection
      .start()
      .then(() => {
        if (!disposed) setLink('live')
      })
      .catch(() => {
        if (!disposed) setLink('offline')
      })

    return () => {
      disposed = true
      void connection.stop()
    }
  }, [])

  return { overview, series, link, error }
}
