import { useState } from 'react'
import type { ReactNode } from 'react'
import RepoDiffCard from '@/components/admin/RepoDiffCard'
import { Kbd } from '@/components/admin/ui/kbd'
import { Button } from '@/components/admin/ui/button'
import type { RepoDiffData, RepoDiffFile } from '@/types'
import '@/styles/admin.css'

const REPO = 'https://github.com/example/fallout'
const BASE = 'a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2'
const HEAD = 'f6e5d4c3b2a1f6e5d4c3b2a1f6e5d4c3b2a1f6e5'

function diff(files: RepoDiffFile[], overrides: Partial<RepoDiffData> = {}): RepoDiffData {
  return {
    commits: 3,
    added: files.filter((f) => f.status === 'added').length,
    modified: files.filter((f) => f.status === 'modified' || f.status === 'changed').length,
    removed: files.filter((f) => f.status === 'removed').length,
    renamed: files.filter((f) => f.status === 'renamed').length,
    files,
    basis: 'sha',
    since: '2026-06-03T12:00:00Z',
    anchor_review_type: 'design_review',
    base_sha: BASE,
    head_sha: HEAD,
    ...overrides,
  }
}

const SMALL: RepoDiffFile[] = [
  { filename: 'README.md', status: 'modified' },
  { filename: 'src/index.ts', status: 'modified' },
  { filename: 'src/new_feature.ts', status: 'added' },
  { filename: 'src/old_thing.ts', status: 'removed' },
  { filename: 'src/util/helpers.ts', status: 'renamed' },
]

const NESTED: RepoDiffFile[] = [
  { filename: 'package.json', status: 'modified' },
  { filename: 'src/components/Button.tsx', status: 'added' },
  { filename: 'src/components/Card.tsx', status: 'modified' },
  { filename: 'src/components/legacy/Old.tsx', status: 'removed' },
  { filename: 'src/hooks/useThing.ts', status: 'added' },
  { filename: 'src/pages/home/index.tsx', status: 'modified' },
  { filename: 'src/pages/home/styles.css', status: 'renamed' },
  { filename: 'docs/architecture/overview.md', status: 'added' },
  { filename: 'docs/architecture/diagrams/flow.svg', status: 'added' },
]

const LARGE: RepoDiffFile[] = Array.from({ length: 40 }, (_, i) => {
  const statuses = ['added', 'modified', 'removed', 'renamed'] as const
  const dirs = ['src/core', 'src/ui/widgets', 'src/ui/widgets/forms', 'lib/parsers', 'tests/unit', 'config']
  return {
    filename: `${dirs[i % dirs.length]}/file_${String(i).padStart(2, '0')}.ts`,
    status: statuses[i % statuses.length],
  }
})

function Scenario({ title, note, children }: { title: string; note?: string; children: ReactNode }) {
  return (
    <section className="space-y-2">
      <div>
        <h2 className="text-sm font-semibold">{title}</h2>
        {note && <p className="text-xs text-muted-foreground">{note}</p>}
      </div>
      {children}
    </section>
  )
}

export default function RepoDiffPreview() {
  const [dark, setDark] = useState(false)
  // `.admin` scopes the shadcn theme tokens; `.dark` on the same wrapper activates dark mode
  // (matches ReviewLayout). Toggle lets us verify both themes in this standalone sandbox.
  return (
    <div className={`admin min-h-screen bg-background text-foreground p-8${dark ? ' dark' : ''}`}>
      <div className="max-w-2xl mx-auto space-y-8">
        <header className="space-y-1">
          <div className="flex items-center justify-between gap-2">
            <h1 className="text-lg font-bold">RepoDiffCard — preview</h1>
            <Button variant="outline" size="sm" onClick={() => setDark((v) => !v)}>
              {dark ? 'Light mode' : 'Dark mode'}
            </Button>
          </div>
          <p className="text-sm text-muted-foreground">
            All scenarios with mock data. Cards start collapsed — click a header to expand the tree. File rows link to
            their per-file diff on GitHub (mock repo, so links 404).
          </p>
        </header>

        <Scenario title="Loading (deferred prop not yet resolved)" note="data === undefined">
          <RepoDiffCard data={undefined} repoLink={REPO} />
        </Scenario>

        <Scenario
          title="Nothing to compare"
          note="data === null — first review, non-GitHub repo, or API failure. Renders nothing (placeholder box below shows where it would be)."
        >
          <div className="rounded-md border border-dashed border-border px-3 py-2 text-xs text-muted-foreground">
            (RepoDiffCard renders null here)
          </div>
          <RepoDiffCard data={null} repoLink={REPO} />
        </Scenario>

        <Scenario title="No changes" note="commits: 0, files: []">
          <RepoDiffCard data={diff([], { commits: 0 })} repoLink={REPO} />
        </Scenario>

        <Scenario title="Small, flat diff" note="mixed statuses, no nesting">
          <RepoDiffCard
            data={diff(SMALL)}
            repoLink={REPO}
            storageKey="preview-small"
            trailing={<Kbd variant="muted">5</Kbd>}
          />
        </Scenario>

        <Scenario title="Nested folders" note="tree view across directories">
          <RepoDiffCard data={diff(NESTED, { commits: 7 })} repoLink={REPO} storageKey="preview-nested" />
        </Scenario>

        <Scenario title="Date-basis fallback" note="basis: 'date' — shows the (approx) marker">
          <RepoDiffCard
            data={diff(SMALL, { basis: 'date', anchor_review_type: 'requirements_check_review' })}
            repoLink={REPO}
            storageKey="preview-date"
          />
        </Scenario>

        <Scenario title="Large diff" note="40 files, deep nesting, scrolls">
          <RepoDiffCard data={diff(LARGE, { commits: 23 })} repoLink={REPO} storageKey="preview-large" />
        </Scenario>
      </div>
    </div>
  )
}
