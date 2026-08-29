import type { ReactNode } from 'react'
import { twMerge } from 'tailwind-merge'
import Frame from '@/components/shared/Frame'

const FrameLayout = ({ children, className }: { children: ReactNode; className?: string }) => (
  <Frame className={twMerge('h-full', className)}>{children}</Frame>
)

export default FrameLayout
