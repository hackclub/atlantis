import type { CSSProperties, ReactNode } from 'react'

type BubbleDirection = 'left' | 'right' | 'bottom' | 'top'

type BubbleProps = {
  text?: string
  children?: ReactNode
  bg?: string
  dir?: BubbleDirection
  showCursor?: boolean
  style?: CSSProperties
}

const tailByDirection: Record<
  BubbleDirection,
  {
    className: string
    width: string
    height: string
    viewBox: string
    outerPoints: string
    innerPoints: string
  }
> = {
  bottom: {
    className: 'absolute -bottom-4 left-1/2 -translate-x-1/2',
    width: '24',
    height: '16',
    viewBox: '0 0 24 16',
    outerPoints: '0,0 24,0 12,16',
    innerPoints: '1,0 23,0 12,14',
  },
  left: {
    className: 'absolute -left-4 top-1/2 -translate-y-1/2',
    width: '16',
    height: '24',
    viewBox: '0 0 16 24',
    outerPoints: '16,0 16,24 0,12',
    innerPoints: '16,1 16,23 2,12',
  },
  right: {
    className: 'absolute -right-4 top-1/2 -translate-y-1/2',
    width: '16',
    height: '24',
    viewBox: '0 0 16 24',
    outerPoints: '0,0 0,24 16,12',
    innerPoints: '0,1 0,23 14,12',
  },
  top: {
    className: 'absolute -top-4 left-1/2 -translate-x-1/2',
    width: '24',
    height: '16',
    viewBox: '0 0 24 16',
    outerPoints: '12,0 24,16 0,16',
    innerPoints: '12,2 23,16 1,16',
  },
}

const SpeechBubble = ({ text, children, bg = 'white', dir = 'bottom', showCursor = false, style }: BubbleProps) => {
  const tail = tailByDirection[dir]
  const rootAlignmentClass = dir === 'left' ? 'items-start' : dir === 'right' ? 'items-end' : 'items-center'
  const textAlignmentClass = dir === 'left' ? 'text-left' : dir === 'right' ? 'text-right' : 'text-center'
  // SVG fill must use inline style — dynamic Tailwind classes like fill-${bg} are not JIT-compiled
  const fillColor = bg === 'white' ? 'white' : `var(--color-${bg})`

  return (
    <div
      className={`relative inline-flex h-auto flex-col rounded-2xl border-2 border-dark-brown bg-${bg} p-3 sm:px-6 sm:py-4 ${rootAlignmentClass}`}
      style={{ width: 'max-content', maxWidth: '100%', ...style }}
    >
      {showCursor && <style>{`@keyframes cursor-blink { 0%, 49% { opacity: 1; } 50%, 100% { opacity: 0; } }`}</style>}
      <span className={`relative z-1 text-base lg:text-lg text-dark-brown font-bold ${textAlignmentClass}`}>
        {children ?? text}
        {showCursor && (
          <span className="inline-block w-[2px] h-[1em] bg-dark-brown ml-0.5 align-middle animate-[cursor-blink_0.8s_steps(1)_infinite]" />
        )}
      </span>

      <svg className={tail.className} width={tail.width} height={tail.height} viewBox={tail.viewBox}>
        <polygon points={tail.outerPoints} fill={fillColor} className="stroke-dark-brown" strokeWidth="2" />
        <polygon points={tail.innerPoints} fill={fillColor} />
      </svg>
    </div>
  )
}

export default SpeechBubble
