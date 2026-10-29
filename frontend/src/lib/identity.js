// UI access follows the /me response bound to the exact token and service.
export function verifiedRole(settings, identity) {
  if (!identity || !settings.pythonToken || identity.token !== settings.pythonToken ||
      identity.backend !== settings.backend || identity.endpoint !== settings.endpoints[settings.backend]) return ''
  return ['user', 'support'].includes(identity.role) ? identity.role : ''
}

export function visibleNavigation(views, role) {
  return views.filter(view => view.id !== 'evaluation' || role === 'support')
}

