import type { ReactNode } from 'react'
import { useForm } from '@inertiajs/react'
import { FormEvent } from 'react'
import AdminLayout from '@/layouts/AdminLayout'
import { Button } from '@/components/admin/ui/button'
import { Card, CardContent } from '@/components/admin/ui/card'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/admin/ui/table'

interface Segment {
  start_min: number
  end_min: number
  duration_min: number
}

interface Results {
  inactive_frames: number
  total_frames: number
  inactive_percentage: number
  segments: Segment[]
}

export default function AdminActivityChecksNew({ results }: { results?: Results }) {
  const { data, setData, post, processing } = useForm<{ video: File | null }>({ video: null })

  function handleSubmit(e: FormEvent) {
    e.preventDefault()
    if (!data.video) return
    post('/admin/activity_checks', { forceFormData: true })
  }

  return (
    <div className="max-w-4xl">
      <h1 className="text-2xl font-semibold tracking-tight mb-1">Timelapse Activity Check</h1>
      <p className="text-sm text-muted-foreground mb-6">
        Upload a timelapse video to analyze frame-by-frame activity. Each frame represents 1 minute.
      </p>

      <Card className="mb-6">
        <CardContent className="pt-6">
          <form onSubmit={handleSubmit} className="flex items-end gap-4">
            <label className="flex-1">
              <span className="block text-sm font-medium mb-1.5">Video file</span>
              <input
                type="file"
                accept="video/*"
                onChange={(e) => setData('video', e.target.files?.[0] ?? null)}
                className="block w-full text-sm border border-input rounded-md p-2 file:mr-4 file:py-1 file:px-3 file:rounded file:border-0 file:text-sm file:font-medium file:bg-secondary file:text-secondary-foreground hover:file:bg-secondary/80"
              />
            </label>
            <Button type="submit" disabled={!data.video || processing}>
              {processing ? 'Analyzing...' : 'Analyze'}
            </Button>
          </form>
        </CardContent>
      </Card>

      {results && <ActivityResults results={results} />}
    </div>
  )
}

function ActivityResults({ results }: { results: Results }) {
  const activePercentage = (100 - results.inactive_percentage).toFixed(1)

  return (
    <div>
      <div className="grid grid-cols-3 gap-4 mb-6">
        <StatCard label="Total Frames" value={results.total_frames} subtitle="1 frame = 1 minute" />
        <StatCard
          label="Active"
          value={`${activePercentage}%`}
          subtitle={`${results.total_frames - results.inactive_frames} active transitions`}
        />
        <StatCard
          label="Inactive"
          value={`${results.inactive_percentage}%`}
          subtitle={`${results.inactive_frames} idle transitions`}
          warn={results.inactive_percentage > 30}
        />
      </div>

      <h2 className="text-lg font-semibold mb-3">Timeline</h2>
      <Timeline totalFrames={results.total_frames} segments={results.segments} />

      {results.segments.length > 0 && (
        <div className="mt-6">
          <h2 className="text-lg font-semibold mb-3">Inactive Segments</h2>
          <div className="rounded-md border border-border">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Start</TableHead>
                  <TableHead>End</TableHead>
                  <TableHead>Duration</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {results.segments.map((seg, i) => (
                  <TableRow key={i}>
                    <TableCell>Minute {seg.start_min}</TableCell>
                    <TableCell>Minute {seg.end_min}</TableCell>
                    <TableCell>{seg.duration_min} min</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        </div>
      )}
    </div>
  )
}

function StatCard({
  label,
  value,
  subtitle,
  warn,
}: {
  label: string
  value: string | number
  subtitle: string
  warn?: boolean
}) {
  return (
    <Card className={warn ? 'border-destructive' : ''}>
      <CardContent className="pt-4 pb-4">
        <div className="text-xs text-muted-foreground">{label}</div>
        <div className="text-2xl font-bold mt-0.5">{value}</div>
        <div className="text-xs text-muted-foreground mt-1">{subtitle}</div>
      </CardContent>
    </Card>
  )
}

function Timeline({ totalFrames, segments }: { totalFrames: number; segments: Segment[] }) {
  if (totalFrames === 0) return null

  const inactiveSet = new Set<number>()
  for (const seg of segments) {
    for (let i = seg.start_min; i <= seg.end_min; i++) {
      inactiveSet.add(i)
    }
  }

  return (
    <div>
      <div className="flex w-full h-6 rounded-md overflow-hidden border border-border">
        {Array.from({ length: totalFrames }, (_, i) => {
          const inactive = inactiveSet.has(i)
          return (
            <div
              key={i}
              className={inactive ? 'bg-destructive/40' : 'bg-green-500'}
              style={{ flex: 1 }}
              title={`Minute ${i}${inactive ? ' (idle)' : ''}`}
            />
          )
        })}
      </div>
      <div className="flex justify-between text-xs text-muted-foreground mt-1">
        <span>0 min</span>
        <span>{totalFrames} min</span>
      </div>
    </div>
  )
}

AdminActivityChecksNew.layout = (page: ReactNode) => <AdminLayout>{page}</AdminLayout>
