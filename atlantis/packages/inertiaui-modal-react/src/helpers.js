export function sameUrlPath(a, b) {
  const origin = typeof window !== 'undefined' ? window.location.origin : 'http://localhost'
  a = typeof a === 'string' ? new URL(a, origin) : a
  b = typeof b === 'string' ? new URL(b, origin) : b
  return `${a.origin}${a.pathname}` === `${b.origin}${b.pathname}`
}

export function generateId(prefix = 'inertiaui_modal_') {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return `${prefix}${crypto.randomUUID()}`
  }
  return `${prefix}${Date.now().toString(36)}_${Math.random().toString(36).substr(2, 9)}`
}

function lower(val) {
  return typeof val === 'string' ? val.toLowerCase() : val
}

export function except(collection, keys, caseInsensitive = false) {
  if (caseInsensitive) keys = keys.map(lower)
  if (Array.isArray(collection)) {
    return collection.filter((item) => !keys.includes(caseInsensitive ? lower(item) : item))
  }
  return Object.keys(collection).reduce((result, key) => {
    if (!keys.includes(caseInsensitive ? lower(key) : key)) {
      result[key] = collection[key]
    }
    return result
  }, {})
}

export function only(collection, keys, caseInsensitive = false) {
  if (caseInsensitive) keys = keys.map(lower)
  if (Array.isArray(collection)) {
    return collection.filter((item) => keys.includes(caseInsensitive ? lower(item) : item))
  }
  return Object.keys(collection).reduce((result, key) => {
    if (keys.includes(caseInsensitive ? lower(key) : key)) {
      result[key] = collection[key]
    }
    return result
  }, {})
}

export function rejectNullValues(collection) {
  if (Array.isArray(collection)) {
    return collection.filter((item) => item !== null)
  }
  return Object.keys(collection).reduce((result, key) => {
    if (key in collection && collection[key] !== null) {
      result[key] = collection[key]
    }
    return result
  }, {})
}

export function kebabCase(str) {
  if (!str) return ''
  str = str.replace(/_/g, '-')
  str = str.replace(/-+/g, '-')
  if (/[A-Z]/.test(str)) {
    str = str
      .replace(/\s+/g, '')
      .replace(/_/g, '')
      .replace(/(?:^|\s|-)+([A-Za-z])/g, (_, c) => c.toUpperCase())
    str = str.replace(/(.)(?=[A-Z])/g, '$1-')
    return str.toLowerCase()
  }
  return str
}

export function isStandardDomEvent(eventName) {
  if (typeof window !== 'undefined') {
    return eventName.toLowerCase() in window
  }
  if (typeof document !== 'undefined') {
    const el = document.createElement('div')
    return eventName.toLowerCase() in el
  }
  const name = eventName.toLowerCase()
  return [
    /^on(click|dblclick|mousedown|mouseup|mouseover|mouseout|mousemove|mouseenter|mouseleave)$/,
    /^on(keydown|keyup|keypress)$/,
    /^on(focus|blur|change|input|submit|reset)$/,
    /^on(load|unload|error|resize|scroll)$/,
    /^on(touchstart|touchend|touchmove|touchcancel)$/,
    /^on(pointerdown|pointerup|pointermove|pointerenter|pointerleave|pointercancel)$/,
    /^on(drag|dragstart|dragend|dragenter|dragleave|dragover|drop)$/,
    /^on(animationstart|animationend|animationiteration)$/,
    /^on(transitionstart|transitionend|transitionrun|transitioncancel)$/,
  ].some((re) => re.test(name))
}
