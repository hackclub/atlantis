import React, { useMemo } from 'react'
import { useModalStack } from './ModalRoot'

const ModalIndexContext = React.createContext(null)
ModalIndexContext.displayName = 'ModalIndexContext'

export const useModalIndex = () => {
  return React.useContext(ModalIndexContext)
}

const ModalRenderer = ({ index }) => {
  const { stack } = useModalStack()

  const modalContext = useMemo(() => {
    return stack[index]
  }, [stack, index])

  return (
    modalContext?.component && (
      <ModalIndexContext.Provider value={index}>
        <modalContext.component {...modalContext.props} onModalEvent={(...args) => modalContext.emit(...args)} />
      </ModalIndexContext.Provider>
    )
  )
}

export default ModalRenderer
