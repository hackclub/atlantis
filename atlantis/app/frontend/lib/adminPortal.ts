// Admin theming (light + .dark) is scoped to AdminLayout's `.admin` wrapper. Radix overlays
// portal to document.body by default, which sits OUTSIDE that subtree — so they'd render with
// none of the admin CSS variables (i.e. always light). Mounting them inside `.admin` keeps the
// theme — including dark mode — intact. Falls back to body (undefined) if the root isn't found.
export function adminPortalContainer(): HTMLElement | undefined {
  if (typeof document === 'undefined') return undefined
  return (document.querySelector('.admin') as HTMLElement | null) ?? undefined
}
