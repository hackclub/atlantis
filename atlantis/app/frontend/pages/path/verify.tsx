import type { ReactNode } from 'react'
import { usePage } from '@inertiajs/react'
import type { SharedProps } from '@/types'
import SignUpCta from '@/components/path/SignUpCta'

export default function Verify() {
  const { sign_in_path } = usePage<SharedProps>().props

  return (
    <div className="min-h-screen flex items-center justify-center">
      <SignUpCta signInPath={sign_in_path} />
    </div>
  )
}

Verify.layout = (page: ReactNode) => page
