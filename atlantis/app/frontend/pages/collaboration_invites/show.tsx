import { useMemo } from 'react'
import { router, Link } from '@inertiajs/react'
import { Modal } from '@inertiaui/modal-react'
import Frame from '@/components/shared/Frame'
import Button from '@/components/shared/Button'
import { notify } from '@/lib/notifications'
import type { InviteDetail } from '@/types'
import type { ReactNode } from 'react'

const GRASS_IMAGES = Array.from({ length: 11 }, (_, i) => `/grass/${i + 1}.svg`)
const GRASS_COUNT = 30

export default function CollaborationInviteShow({ invite, is_modal }: { invite: InviteDetail; is_modal?: boolean }) {
  function handleAccept() {
    router.post(
      `/collaboration_invites/${invite.id}/accept`,
      {},
      {
        onError: () => notify('alert', 'Failed to accept invite.'),
      },
    )
  }

  function handleDecline() {
    if (confirm('Are you sure you want to decline this invite?')) {
      router.post(
        `/collaboration_invites/${invite.id}/decline`,
        {},
        {
          onError: () => notify('alert', 'Failed to decline invite.'),
        },
      )
    }
  }

  const content = (
    <div className="flex flex-col items-center text-center text-dark-brown p-6">
      <img src={invite.inviter_avatar} alt="" className="w-16 h-16 rounded-full border-2 border-dark-brown mb-4" />

      {invite.status === 'pending' && (
        <>
          <p className="text-lg mb-6">
            <span className="font-bold">{invite.inviter_display_name}</span> has invited you to collaborate on{' '}
            <span className="font-bold">{invite.project_name}</span>
          </p>
          <div className="flex gap-3">
            <Button onClick={handleAccept}>Accept</Button>
            <Button onClick={handleDecline} className="bg-dark-brown">
              Decline
            </Button>
          </div>
        </>
      )}

      {invite.status === 'accepted' && (
        <>
          <p className="text-lg mb-4">
            You accepted <span className="font-bold">{invite.inviter_display_name}</span>&apos;s invite to collaborate
            on <span className="font-bold">{invite.project_name}</span>.
          </p>
          <Link href="/path" className="text-brown underline">
            Go to path
          </Link>
        </>
      )}

      {invite.status === 'declined' && (
        <p className="text-lg">
          You declined <span className="font-bold">{invite.inviter_display_name}</span>&apos;s invite to{' '}
          <span className="font-bold">{invite.project_name}</span>.
        </p>
      )}

      {invite.status === 'revoked' && <p className="text-lg">This invite was withdrawn.</p>}
    </div>
  )

  if (is_modal) {
    return (
      <Modal paddingClasses="max-w-lg mx-auto" closeButton={false}>
        <Frame showBorderOnMobile>{content}</Frame>
      </Modal>
    )
  }

  return (
    <PageWrapper>
      <Frame showBorderOnMobile>{content}</Frame>
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
      <div className="relative z-10 min-h-screen flex items-center justify-center p-4">{children}</div>
    </div>
  )
}

CollaborationInviteShow.layout = (page: ReactNode) => page
