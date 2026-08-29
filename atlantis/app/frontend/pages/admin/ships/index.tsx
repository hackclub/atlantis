import type { ReactNode } from 'react'
import { Link, Deferred } from '@inertiajs/react'
import type { ColumnDef } from '@tanstack/react-table'
import AdminLayout from '@/layouts/AdminLayout'
import { Badge } from '@/components/admin/ui/badge'
import { DataTable } from '@/components/admin/DataTable'
import { DataTableSkeleton } from '@/components/admin/DataTableSkeleton'
import type { AdminShipRow, PagyProps } from '@/types'

const statusColors: Record<string, 'default' | 'secondary' | 'destructive' | 'outline'> = {
  pending: 'secondary',
  approved: 'default',
  rejected: 'destructive',
}

const columns: ColumnDef<AdminShipRow>[] = [
  {
    accessorKey: 'id',
    header: 'ID',
    cell: ({ row }) => (
      <Link
        href={`/admin/reviews/${row.original.id}`}
        prefetch
        cacheFor="30s"
        className="text-muted-foreground hover:underline"
      >
        {row.original.id}
      </Link>
    ),
  },
  {
    accessorKey: 'project_name',
    header: 'Project',
    cell: ({ row }) => <span className="font-medium">{row.original.project_name}</span>,
  },
  {
    accessorKey: 'user_display_name',
    header: 'Owner',
  },
  {
    accessorKey: 'status',
    header: 'Status',
    cell: ({ row }) => (
      <Badge variant={statusColors[row.original.status] ?? 'outline'} className="capitalize">
        {row.original.status}
      </Badge>
    ),
  },
  {
    accessorKey: 'reviewer_display_name',
    header: 'Reviewer',
    cell: ({ row }) => row.original.reviewer_display_name ?? <span className="text-muted-foreground">Unassigned</span>,
  },
  {
    accessorKey: 'created_at',
    header: 'Created',
  },
]

export default function AdminShipsIndex({ ships, pagy }: { ships?: AdminShipRow[]; pagy?: PagyProps }) {
  return (
    <div>
      <h1 className="text-2xl font-semibold tracking-tight mb-4">Reviews</h1>
      <Deferred data={['ships', 'pagy']} fallback={<DataTableSkeleton columns={columns.length} />}>
        <DataTable columns={columns} data={ships ?? []} pagy={pagy} noun="reviews" />
      </Deferred>
    </div>
  )
}

AdminShipsIndex.layout = (page: ReactNode) => <AdminLayout>{page}</AdminLayout>
