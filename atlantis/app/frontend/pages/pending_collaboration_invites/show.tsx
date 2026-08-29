import { useMemo, type ReactNode } from 'react'
import { router, usePage } from '@inertiajs/react'
import Frame from '@/components/shared/Frame'
import Button from '@/components/shared/Button'
import type { SharedProps } from '@/types'

const GRASS_IMAGES = Array.from({ length: 11 }, (_, i) => `/grass/${i + 1}.svg`)
const GRASS_COUNT = 30

type Props = {
  state: 'unauthenticated' | 'trial' | 'revoked' | 'wrong_user'
  inviter_name?: string
  inviter_avatar?: string
  project_name: string
  sign_in_path?: string
  trial_session_path?: string
}

export default function PendingCollaborationInviteShow({
  state,
  inviter_name,
  inviter_avatar,
  project_name,
  sign_in_path,
  trial_session_path,
}: Props) {
  const shared = usePage<SharedProps>().props

  if (state === 'revoked') {
    return (
      <PageWrapper>
        <p className="text-lg">
          The collaboration invite for <span className="font-bold">{project_name}</span> has been withdrawn.
        </p>
        <a href="/" className="text-brown underline mt-4">
          Go home
        </a>
      </PageWrapper>
    )
  }

  if (state === 'wrong_user') {
    return (
      <PageWrapper>
        <p className="text-lg">
          This invite was sent to a different email address. Please sign in with the email this invite was sent to.
        </p>
        <a
          href={shared.sign_out_path}
          onClick={(e) => {
            e.preventDefault()
            router.delete(shared.sign_out_path)
          }}
          className="text-brown underline mt-4"
        >
          Sign out and switch accounts
        </a>
      </PageWrapper>
    )
  }

  if (state === 'trial') {
    return (
      <PageWrapper>
        <InviteHeader inviterName={inviter_name} inviterAvatar={inviter_avatar} projectName={project_name} />
        <p className="mb-6">Verify your account to accept this invite.</p>
        <a
          href={sign_in_path}
          className="py-1.5 px-4 bg-brown text-light-brown border-2 border-dark-brown font-bold uppercase text-center block"
        >
          Verify with Hack Club
        </a>
      </PageWrapper>
    )
  }

  // state === 'unauthenticated'
  return (
    <PageWrapper>
      <InviteHeader inviterName={inviter_name} inviterAvatar={inviter_avatar} projectName={project_name} />
      <div className="flex flex-col items-center gap-3 w-full">
        <a
          href={sign_in_path}
          className="py-1.5 px-4 bg-brown text-light-brown border-2 border-dark-brown font-bold uppercase text-center block w-full"
        >
          Sign in with Hack Club
        </a>
        <TrialSignupForm trialSessionPath={trial_session_path!} />
      </div>
    </PageWrapper>
  )
}

function PageWrapper({ children }: { children: ReactNode }) {
  const grassBlades = useMemo(
    () =>
      Array.from({ length: GRASS_COUNT }, (_, i) => ({
        id: i,
        src: GRASS_IMAGES[i % GRASS_IMAGES.length],
        left: Math.random() * 100,
        top: Math.random() * 100,
        scale: 0.4 + Math.random() * 0.4,
        rotation: (Math.random() - 0.5) * 30,
        flipX: Math.random() > 0.5,
      })),
    [],
  )

  return (
    <div className="min-h-screen relative overflow-hidden">
      {/* Sky */}
      <div className="fixed top-0 left-0 right-0 h-[40vh] bg-light-blue overflow-hidden">
        <img src="/clouds/4.webp" alt="" className="absolute bottom-0 left-0 h-full -translate-x-1/3" />
        <img src="/clouds/1.webp" alt="" className="absolute bottom-0 left-40 h-full translate-x-1/3" />
        <img src="/clouds/2.webp" alt="" className="absolute bottom-0 right-0 -translate-x-5/6 h-full" />
        <img src="/clouds/3.webp" alt="" className="absolute bottom-0 right-0 h-full translate-x-1/3" />
      </div>

      {/* Ground */}
      <div className="fixed top-[40vh] left-0 right-0 bottom-0 bg-light-green">
        {grassBlades.map((g) => (
          <img
            key={g.id}
            src={g.src}
            alt=""
            className="absolute pointer-events-none select-none"
            style={{
              left: `${g.left}%`,
              top: `${g.top}%`,
              width: 40,
              height: 60,
              transform: `translate(-50%, -50%) scale(${g.flipX ? -g.scale : g.scale}, ${g.scale}) rotate(${g.rotation}deg)`,
            }}
          />
        ))}
      </div>

      {/* Card */}
      <div className="relative z-10 min-h-screen flex items-center justify-center p-4 text-dark-brown">
        <Frame showBorderOnMobile>
          <div className="flex flex-col items-center text-center p-6 max-w-sm">{children}</div>
        </Frame>
      </div>
    </div>
  )
}

function InviteHeader({
  inviterName,
  inviterAvatar,
  projectName,
}: {
  inviterName?: string
  inviterAvatar?: string
  projectName: string
}) {
  return (
    <div className="flex flex-col items-center mb-6">
      {inviterAvatar && (
        <img src={inviterAvatar} alt="" className="w-16 h-16 rounded-full border-2 border-dark-brown mb-4" />
      )}
      <p className="text-lg">
        <span className="font-bold">{inviterName}</span> has invited you to collaborate on{' '}
        <span className="font-bold">{projectName}</span>
      </p>
    </div>
  )
}

function TrialSignupForm({ trialSessionPath }: { trialSessionPath: string }) {
  const csrfToken =
    typeof document !== 'undefined' ? document.querySelector('meta[name="csrf-token"]')?.getAttribute('content') : ''

  return (
    <form method="post" action={trialSessionPath} className="w-full">
      <input type="hidden" name="authenticity_token" value={csrfToken || ''} />
      <p className="text-sm mb-2">or continue with email</p>
      <div className="flex gap-2">
        <input
          type="email"
          name="email"
          placeholder="you@example.com"
          required
          className="flex-1 min-w-0 py-1.5 px-3 border-2 border-dark-brown bg-light-brown text-dark-brown placeholder-brown outline-none"
        />
        <Button type="submit">Go</Button>
      </div>
    </form>
  )
}

PendingCollaborationInviteShow.layout = (page: ReactNode) => page
