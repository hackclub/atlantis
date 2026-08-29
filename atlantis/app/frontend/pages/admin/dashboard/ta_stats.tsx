import { useState } from 'react'
import type { ReactNode } from 'react'
import { Link, usePage } from '@inertiajs/react'
import AdminLayout from '@/layouts/AdminLayout'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/admin/ui/card'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/admin/ui/table'
import { Badge } from '@/components/admin/ui/badge'
import { Sheet, SheetContent, SheetHeader, SheetTitle } from '@/components/admin/ui/sheet'
import { PageProps } from '@inertiajs/core'

interface ReviewerRow {
  id: number
  display_name: string
  avg_deflation: number
  hours_reviewed: number
  ships_reviewed: number
  projects_reviewed: number
}

interface OwnerShip {
  ship_id: number
  project_id: number
  project_name: string
  reviewer_id: number
  reviewer_display_name: string | null
  deflation: number
  hours: number
  reviewed_at: string | null
}

interface OwnerRow {
  id: number
  display_name: string
  avatar: string | null
  ship_count: number
  reviewer_count: number
  avg_deflation: number
  min_deflation: number
  max_deflation: number
  spread: number
  stddev: number
  ships: OwnerShip[]
}

interface Props extends PageProps {
  reviewers: ReviewerRow[]
  owners: OwnerRow[]
}

function pct(value: number): string {
  return `${Math.round(value * 1000) / 10}%`
}

export default function TaStatsDashboard() {
  const { reviewers, owners } = usePage<Props>().props
  const [ownerSheet, setOwnerSheet] = useState<OwnerRow | null>(null)

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">TA Stats</h1>
        <p className="text-sm text-muted-foreground">Time-audit deflation, raw data only — no thresholds applied</p>
      </div>

      <div>
        <h2 className="text-lg font-semibold tracking-tight mb-1">Deflation by Reviewer</h2>
        <p className="text-sm text-muted-foreground mb-4">
          Time-weighted deflation rate (total removed ÷ total raw). Should converge across reviewers at a large sample —
          hours reviewed is that sample size.
        </p>
        <Card>
          <CardContent>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="w-12">Rank</TableHead>
                  <TableHead>Reviewer</TableHead>
                  <TableHead className="text-right">Deflation</TableHead>
                  <TableHead className="text-right">Hours Reviewed</TableHead>
                  <TableHead className="text-right">Ships</TableHead>
                  <TableHead className="text-right">Projects</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {reviewers.length === 0 ? (
                  <TableRow>
                    <TableCell colSpan={6} className="text-center text-muted-foreground py-8">
                      No data :(
                    </TableCell>
                  </TableRow>
                ) : (
                  reviewers.map((row, index) => (
                    <TableRow key={row.id}>
                      <TableCell className="font-medium text-muted-foreground">{index + 1}</TableCell>
                      <TableCell>
                        <Link href={`/admin/reviewers/${row.id}`} className="font-medium hover:underline">
                          {row.display_name}
                        </Link>
                      </TableCell>
                      <TableCell className="text-right tabular-nums">{pct(row.avg_deflation)}</TableCell>
                      <TableCell className="text-right tabular-nums">{row.hours_reviewed}</TableCell>
                      <TableCell className="text-right tabular-nums">{row.ships_reviewed}</TableCell>
                      <TableCell className="text-right tabular-nums">{row.projects_reviewed}</TableCell>
                    </TableRow>
                  ))
                )}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      </div>

      <div>
        <h2 className="text-lg font-semibold tracking-tight mb-1">Deflation Spread by Project Owner</h2>
        <p className="text-sm text-muted-foreground mb-4">
          Owners with ≥2 audited ships. A large spread — especially across multiple reviewers — means the same person's
          ships were deflated inconsistently. Click a row for the per-ship breakdown.
        </p>
        <Card>
          <CardContent>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Owner</TableHead>
                  <TableHead className="text-right">Ships</TableHead>
                  <TableHead className="text-right">Reviewers</TableHead>
                  <TableHead className="text-right">Avg</TableHead>
                  <TableHead className="text-right">Min</TableHead>
                  <TableHead className="text-right">Max</TableHead>
                  <TableHead className="text-right">Spread</TableHead>
                  <TableHead className="text-right">Std Dev</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {owners.length === 0 ? (
                  <TableRow>
                    <TableCell colSpan={8} className="text-center text-muted-foreground py-8">
                      No data :(
                    </TableCell>
                  </TableRow>
                ) : (
                  owners.map((row) => (
                    <TableRow
                      key={row.id}
                      className="cursor-pointer hover:bg-muted/50"
                      onClick={() => setOwnerSheet(row)}
                    >
                      <TableCell>
                        <div className="flex items-center gap-3">
                          {row.avatar ? (
                            <img src={row.avatar} className="size-8 rounded-full shrink-0" alt="" />
                          ) : (
                            <div className="size-8 rounded-full bg-muted shrink-0" />
                          )}
                          <span className="font-medium">{row.display_name}</span>
                        </div>
                      </TableCell>
                      <TableCell className="text-right tabular-nums">{row.ship_count}</TableCell>
                      <TableCell className="text-right">
                        {row.reviewer_count > 1 ? (
                          <Badge variant="secondary">{row.reviewer_count}</Badge>
                        ) : (
                          <span className="tabular-nums text-muted-foreground">{row.reviewer_count}</span>
                        )}
                      </TableCell>
                      <TableCell className="text-right tabular-nums">{pct(row.avg_deflation)}</TableCell>
                      <TableCell className="text-right tabular-nums">{pct(row.min_deflation)}</TableCell>
                      <TableCell className="text-right tabular-nums">{pct(row.max_deflation)}</TableCell>
                      <TableCell className="text-right tabular-nums font-medium">{pct(row.spread)}</TableCell>
                      <TableCell className="text-right tabular-nums">{pct(row.stddev)}</TableCell>
                    </TableRow>
                  ))
                )}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      </div>

      <Sheet open={ownerSheet !== null} onOpenChange={(open) => !open && setOwnerSheet(null)}>
        <SheetContent>
          <SheetHeader>
            <SheetTitle>Ship deflation — {ownerSheet?.display_name}</SheetTitle>
          </SheetHeader>
          <div className="mt-4 space-y-2">
            {ownerSheet?.ships.map((ship) => (
              <div key={ship.ship_id} className="rounded border px-3 py-2 text-sm">
                <div className="flex items-center justify-between gap-2">
                  <Link href={`/admin/projects/${ship.project_id}`} className="font-medium hover:underline truncate">
                    {ship.project_name}
                  </Link>
                  <span className="tabular-nums font-medium shrink-0">{pct(ship.deflation)}</span>
                </div>
                <div className="mt-1 flex items-center justify-between gap-2 text-xs text-muted-foreground">
                  <span>
                    {ship.reviewer_display_name ? (
                      <Link href={`/admin/reviewers/${ship.reviewer_id}`} className="hover:underline">
                        {ship.reviewer_display_name}
                      </Link>
                    ) : (
                      '—'
                    )}
                    {ship.reviewed_at ? ` · ${ship.reviewed_at}` : ''}
                  </span>
                  <span className="tabular-nums shrink-0">{ship.hours}h</span>
                </div>
              </div>
            ))}
          </div>
        </SheetContent>
      </Sheet>
    </div>
  )
}

TaStatsDashboard.layout = (page: ReactNode) => <AdminLayout>{page}</AdminLayout>
