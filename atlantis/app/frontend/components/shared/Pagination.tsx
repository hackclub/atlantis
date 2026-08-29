import { Children, type ReactNode, useState } from 'react'

interface PaginationContext {
  next: () => void
  prev: () => void
  currentPage: number
  totalPages: number
}

export function PaginationPage({ children }: { children: (ctx: PaginationContext) => ReactNode }) {
  return null
}

export function Pagination({ children, className }: { children: ReactNode; className?: string }) {
  const pages = Children.toArray(children)
  const [currentPage, setCurrentPage] = useState(0)

  const ctx: PaginationContext = {
    next: () => setCurrentPage((p) => Math.min(p + 1, pages.length - 1)),
    prev: () => setCurrentPage((p) => Math.max(p - 1, 0)),
    currentPage,
    totalPages: pages.length,
  }

  const activePage = pages[currentPage] as React.ReactElement<{
    children: (ctx: PaginationContext) => ReactNode
  }>

  if (!activePage) return null

  return <div className={className}>{activePage.props.children(ctx)}</div>
}
