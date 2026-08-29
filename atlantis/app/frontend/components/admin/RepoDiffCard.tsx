import { useState, useMemo, useEffect } from 'react'
import type { ReactNode } from 'react'
import { ChevronDownIcon, ChevronRightIcon, FolderIcon, ArrowUpRightIcon, GitCommitHorizontalIcon } from 'lucide-react'
import type { RepoDiffData, RepoDiffFile } from '@/types'

const STATUS_STYLES: Record<string, { sign: string; cls: string }> = {
  added: { sign: '+', cls: 'text-emerald-600 dark:text-emerald-400' },
  modified: { sign: '~', cls: 'text-amber-600 dark:text-amber-400' },
  changed: { sign: '~', cls: 'text-amber-600 dark:text-amber-400' },
  removed: { sign: '−', cls: 'text-red-600 dark:text-red-400' },
  renamed: { sign: '→', cls: 'text-blue-600 dark:text-blue-400' },
}

interface DiffNode {
  name: string
  path: string
  type: 'tree' | 'blob'
  status?: string
  children: DiffNode[]
}

function buildDiffTree(files: RepoDiffFile[]): DiffNode[] {
  const root: DiffNode[] = []
  const dirs = new Map<string, DiffNode>()

  const ensureDir = (path: string): DiffNode | null => {
    if (!path) return null
    const existing = dirs.get(path)
    if (existing) return existing
    const parts = path.split('/')
    const name = parts[parts.length - 1]
    const node: DiffNode = { name, path, type: 'tree', children: [] }
    dirs.set(path, node)
    const parent = ensureDir(parts.slice(0, -1).join('/'))
    ;(parent ? parent.children : root).push(node)
    return node
  }

  for (const file of files) {
    const parts = file.filename.split('/')
    const name = parts[parts.length - 1]
    const node: DiffNode = { name, path: file.filename, type: 'blob', status: file.status, children: [] }
    const parent = ensureDir(parts.slice(0, -1).join('/'))
    ;(parent ? parent.children : root).push(node)
  }

  const sort = (nodes: DiffNode[]) => {
    nodes.sort((a, b) => {
      if (a.type !== b.type) return a.type === 'tree' ? -1 : 1
      return a.name < b.name ? -1 : a.name > b.name ? 1 : 0
    })
    for (const n of nodes) if (n.children.length) sort(n.children)
  }
  sort(root)
  return root
}

async function sha256Hex(input: string): Promise<string> {
  const buf = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(input))
  return Array.from(new Uint8Array(buf))
    .map((b) => b.toString(16).padStart(2, '0'))
    .join('')
}

function DiffFolder({ node, depth, fileHref }: { node: DiffNode; depth: number; fileHref: (path: string) => string }) {
  const [open, setOpen] = useState(true)
  return (
    <div>
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-1 w-full text-left py-0.5 hover:bg-muted/50 rounded transition-colors cursor-pointer"
        style={{ paddingLeft: `${depth * 16 + 4}px` }}
      >
        {open ? (
          <ChevronDownIcon className="size-3 shrink-0 text-muted-foreground" />
        ) : (
          <ChevronRightIcon className="size-3 shrink-0 text-muted-foreground" />
        )}
        <FolderIcon className="size-3.5 shrink-0 text-blue-500 dark:text-blue-400" />
        <span className="truncate">{node.name}</span>
      </button>
      {open &&
        node.children.map((child) =>
          child.type === 'tree' ? (
            <DiffFolder key={child.path} node={child} depth={depth + 1} fileHref={fileHref} />
          ) : (
            <DiffFile key={child.path} node={child} depth={depth + 1} fileHref={fileHref} />
          ),
        )}
    </div>
  )
}

function DiffFile({ node, depth, fileHref }: { node: DiffNode; depth: number; fileHref: (path: string) => string }) {
  const style = STATUS_STYLES[node.status ?? ''] ?? { sign: '?', cls: 'text-muted-foreground' }
  return (
    <a
      href={fileHref(node.path)}
      target="_blank"
      rel="noopener noreferrer"
      className="flex items-center gap-1.5 py-0.5 rounded transition-colors w-full text-left cursor-pointer hover:bg-muted/50"
      style={{ paddingLeft: `${depth * 16 + 4 + 12}px` }}
    >
      <span className={`shrink-0 w-3 text-center font-semibold ${style.cls}`}>{style.sign}</span>
      <span
        className={`truncate ${node.status === 'removed' ? 'text-muted-foreground line-through' : 'text-foreground'}`}
      >
        {node.name}
      </span>
    </a>
  )
}

function formatSince(iso: string | null): string | null {
  if (!iso) return null
  return new Date(iso).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' })
}

export default function RepoDiffCard({
  data,
  repoLink,
  storageKey,
  trailing,
}: {
  // undefined = deferred prop still loading; null = nothing to compare (first review, non-GitHub, or API down)
  data: RepoDiffData | null | undefined
  repoLink: string
  storageKey?: string
  trailing?: ReactNode
}) {
  const [open, setOpen] = useState(() => {
    if (storageKey) {
      try {
        const saved = localStorage.getItem(`collapsible:${storageKey}`)
        if (saved !== null) return saved === '1'
      } catch {}
    }
    return false
  })

  // GitHub anchors each file's diff as `diff-<sha256(path)>` in the compare view.
  const [anchors, setAnchors] = useState<Record<string, string>>({})
  const files = data?.files
  useEffect(() => {
    if (!files) return
    let cancelled = false
    Promise.all(files.map(async (f) => [f.filename, `diff-${await sha256Hex(f.filename)}`] as const)).then((pairs) => {
      if (!cancelled) setAnchors(Object.fromEntries(pairs))
    })
    return () => {
      cancelled = true
    }
  }, [files])

  const tree = useMemo(() => (files ? buildDiffTree(files) : []), [files])

  if (data === undefined) {
    return (
      <div className="rounded-md border border-border overflow-hidden">
        <div className="w-full flex items-center gap-2 px-3 py-2 bg-muted/50 text-left">
          <span className="text-sm font-semibold shrink-0">Changes Since Last Review</span>
          <span className="text-xs text-muted-foreground flex-1">Loading…</span>
        </div>
      </div>
    )
  }
  if (data === null) return null

  const toggle = () =>
    setOpen((v) => {
      const next = !v
      if (storageKey) {
        try {
          localStorage.setItem(`collapsible:${storageKey}`, next ? '1' : '0')
        } catch {}
      }
      return next
    })

  const githubBase = repoLink.replace(/\/+$/, '').replace(/\/tree\/[^/]+$/, '')
  const compareUrl = `${githubBase}/compare/${data.base_sha}...${data.head_sha}`
  const fileHref = (path: string) => (anchors[path] ? `${compareUrl}#${anchors[path]}` : compareUrl)
  const since = formatSince(data.since)
  const noChanges = data.files.length === 0 && data.commits === 0

  const summary = noChanges ? (
    <span>No file changes{since ? ` since ${since}` : ''}</span>
  ) : (
    <span className="flex items-center gap-2">
      <span className="flex items-center gap-1">
        <GitCommitHorizontalIcon className="size-3.5" />
        {data.commits} commit{data.commits === 1 ? '' : 's'}
      </span>
      <span>
        {data.added > 0 && <span className="text-emerald-600 dark:text-emerald-400">+{data.added} </span>}
        {data.modified > 0 && <span className="text-amber-600 dark:text-amber-400">~{data.modified} </span>}
        {data.removed > 0 && <span className="text-red-600 dark:text-red-400">−{data.removed} </span>}
        {data.renamed > 0 && <span className="text-blue-600 dark:text-blue-400">→{data.renamed}</span>}
      </span>
      {since && <span className="text-muted-foreground">since {since}</span>}
      {data.basis === 'date' && (
        <span
          className="text-muted-foreground italic"
          title="The exact reviewed commit wasn't available (force-pushed or pre-dates tracking); diffed from the prior review's completion date instead."
        >
          (approx)
        </span>
      )}
    </span>
  )

  return (
    <div className="rounded-md border border-border overflow-hidden">
      <button
        onClick={toggle}
        className="w-full flex items-center gap-2 px-3 py-2 bg-muted/50 hover:bg-muted/80 transition-colors cursor-pointer text-left"
        data-card-key={storageKey}
      >
        <span className="text-sm font-semibold shrink-0">Changes Since Last Review</span>
        <span className="text-xs text-muted-foreground flex-1 min-w-0 truncate">{summary}</span>
        <a
          href={compareUrl}
          target="_blank"
          rel="noopener noreferrer"
          onClick={(e) => e.stopPropagation()}
          className="inline-flex items-center gap-1 rounded border border-foreground bg-foreground px-2 py-0.5 text-xs font-semibold text-background hover:opacity-80 transition-opacity"
        >
          GitHub Diff
          <ArrowUpRightIcon className="size-3" />
        </a>
        {trailing}
        <ChevronDownIcon
          className={`size-3.5 shrink-0 text-muted-foreground transition-transform ${open ? '' : '-rotate-90'}`}
        />
      </button>
      {open && (
        <div className="border-t border-border max-h-96 overflow-y-auto p-2 text-xs">
          {noChanges ? (
            <p className="text-muted-foreground px-1 py-2">Nothing changed in the repo since the last review.</p>
          ) : (
            tree.map((node) =>
              node.type === 'tree' ? (
                <DiffFolder key={node.path} node={node} depth={0} fileHref={fileHref} />
              ) : (
                <DiffFile key={node.path} node={node} depth={0} fileHref={fileHref} />
              ),
            )
          )}
        </div>
      )}
    </div>
  )
}
