import { Skeleton } from '@/components/admin/ui/skeleton'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/admin/ui/table'

interface DataTableSkeletonProps {
  columns: number
  rows?: number
  headers?: string[]
  // Match the first column to tables whose first cell is taller than one line, so the deferred
  // swap-in is shift-free:
  //   'avatar'  → avatar circle + name line + sub-line (e.g. koi ledger user cell)
  //   'twoLine' → name line + sub-line, no avatar (e.g. shop orders user/email cell)
  // Omit for plain single-line tables.
  firstColumnVariant?: 'avatar' | 'twoLine'
}

// Placeholder shown via Inertia <Deferred> while a list page's data prop loads.
// Matches DataTable's bordered shell so the swap-in is shift-free.
export function DataTableSkeleton({ columns, rows = 10, headers, firstColumnVariant }: DataTableSkeletonProps) {
  const colCount = headers?.length ?? columns
  return (
    <div>
      <div className="overflow-hidden rounded-md border border-border">
        <Table>
          <TableHeader>
            <TableRow>
              {Array.from({ length: colCount }).map((_, i) => (
                <TableHead key={i}>{headers ? headers[i] : <Skeleton className="h-4 w-16" />}</TableHead>
              ))}
            </TableRow>
          </TableHeader>
          <TableBody>
            {Array.from({ length: rows }).map((_, r) => (
              <TableRow key={r}>
                {Array.from({ length: colCount }).map((_, c) =>
                  firstColumnVariant && c === 0 ? (
                    <TableCell key={c}>
                      {/* Two-line (± avatar) cell ≈ 36px tall, matching a real user/entity cell. */}
                      <div className="flex items-center gap-2.5">
                        {firstColumnVariant === 'avatar' && <Skeleton className="size-7 shrink-0 rounded-full" />}
                        <div className="space-y-2">
                          <Skeleton className="h-4 w-28" />
                          <Skeleton className="h-3 w-36" />
                        </div>
                      </div>
                    </TableCell>
                  ) : (
                    <TableCell key={c}>
                      {/* h-5 line-box matches a single text-sm row so the cell height is exact. */}
                      <div className="flex h-5 items-center">
                        <Skeleton className="h-4" style={{ width: `${55 + ((r * 7 + c * 13) % 40)}%` }} />
                      </div>
                    </TableCell>
                  ),
                )}
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
      <div className="flex items-center justify-between pt-4">
        <Skeleton className="h-4 w-32" />
      </div>
    </div>
  )
}
