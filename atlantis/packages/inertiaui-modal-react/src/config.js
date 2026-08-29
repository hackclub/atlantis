const defaults = {
  type: 'modal',
  navigate: false,
  modal: {
    closeButton: true,
    closeExplicitly: false,
    maxWidth: '2xl',
    paddingClasses: 'p-4 sm:p-6',
    panelClasses: 'bg-white rounded',
    position: 'center',
  },
  slideover: {
    closeButton: true,
    closeExplicitly: false,
    maxWidth: 'md',
    paddingClasses: 'p-4 sm:p-6',
    panelClasses: 'bg-white min-h-screen',
    position: 'right',
  },
}

class Config {
  constructor() {
    this.config = {}
    this.reset()
  }

  reset() {
    this.config = JSON.parse(JSON.stringify(defaults))
  }

  put(key, value) {
    if (typeof key === 'object') {
      this.config = {
        type: key.type ?? defaults.type,
        navigate: key.navigate ?? defaults.navigate,
        modal: { ...defaults.modal, ...(key.modal ?? {}) },
        slideover: { ...defaults.slideover, ...(key.slideover ?? {}) },
      }
      return
    }

    const parts = key.split('.')
    let obj = this.config
    for (let i = 0; i < parts.length - 1; i++) {
      obj = obj[parts[i]] = obj[parts[i]] || {}
    }
    obj[parts[parts.length - 1]] = value
  }

  get(key) {
    if (typeof key === 'undefined') {
      return this.config
    }

    const parts = key.split('.')
    let obj = this.config
    for (const part of parts) {
      if (obj[part] === undefined) return null
      obj = obj[part]
    }
    return obj
  }
}

const instance = new Config()

export const resetConfig = () => instance.reset()
export const putConfig = (key, value) => instance.put(key, value)
export const getConfig = (key) => instance.get(key)
export const getConfigByType = (isSlideover, key) => instance.get(isSlideover ? `slideover.${key}` : `modal.${key}`)
