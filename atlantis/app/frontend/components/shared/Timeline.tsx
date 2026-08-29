import type { ReactNode } from 'react'

const AVATAR_SIZE = 20
const AVATAR_OFFSET = 16
const LINE_LEFT = AVATAR_OFFSET + AVATAR_SIZE / 2

function TimelineItemBase({
  header,
  children,
  isLast = false,
}: {
  header: ReactNode
  children?: ReactNode
  isLast?: boolean
}) {
  return (
    <div>
      <div className="border border-dark-brown rounded bg-beige p-4">
        <div className="text-sm text-dark-brown">{header}</div>
        {children && (
          <>
            <div className="border-t border-brown mt-3 mb-3 -mx-4" />
            <div>{children}</div>
          </>
        )}
      </div>
      {!isLast && (
        <div className="h-6" style={{ paddingLeft: LINE_LEFT }}>
          <div className="w-px h-full bg-dark-brown" />
        </div>
      )}
    </div>
  )
}

function SimpleItem({ header, isLast }: { header: ReactNode; isLast?: boolean }) {
  return <TimelineItemBase header={header} isLast={isLast} />
}

function DetailItem({ header, children, isLast }: { header: ReactNode; children: ReactNode; isLast?: boolean }) {
  return (
    <TimelineItemBase header={header} isLast={isLast}>
      {children}
    </TimelineItemBase>
  )
}

function TimelineLeadIn() {
  return (
    <div className="h-6" style={{ paddingLeft: LINE_LEFT }}>
      <div className="w-px h-full bg-linear-to-b from-transparent to-dark-brown" />
    </div>
  )
}

function Timeline({ children }: { children: ReactNode }) {
  return (
    <div>
      <TimelineLeadIn />
      {children}
    </div>
  )
}

Timeline.SimpleItem = SimpleItem
Timeline.DetailItem = DetailItem

export default Timeline
