export type NotificationType = 'alert' | 'notice'

export type NotificationPayload = {
  id: string
  type: NotificationType
  message: string
}

type Listener = (n: NotificationPayload) => void

const listeners = new Set<Listener>()
const pending: NotificationPayload[] = []
let counter = 0

export function notify(type: NotificationType, message: string) {
  const payload: NotificationPayload = { id: `notification-${counter++}`, type, message }
  if (listeners.size === 0) {
    pending.push(payload)
  } else {
    listeners.forEach((l) => l(payload))
  }
}

export function subscribe(fn: Listener): () => void {
  listeners.add(fn)
  pending.splice(0).forEach((p) => fn(p))
  return () => listeners.delete(fn)
}
