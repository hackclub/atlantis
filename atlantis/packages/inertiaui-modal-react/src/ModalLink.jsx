import { useCallback, useState, useEffect, useMemo } from 'react'
import { useModalStack, modalPropNames } from './ModalRoot'
import { useModalIndex } from './ModalRenderer'
import { only, rejectNullValues, isStandardDomEvent } from './helpers'
import { getConfig } from './config'

const ModalLink = ({
  href,
  method = 'get',
  data = {},
  as: Component = 'a',
  headers = {},
  queryStringArrayFormat = 'brackets',
  onAfterLeave = null,
  onBlur = null,
  onClose = null,
  onError = null,
  onFocus = null,
  onStart = null,
  onSuccess = null,
  navigate = null,
  replace = false,
  children,
  ...props
}) => {
  const [loading, setLoading] = useState(false)
  const [modalContext, setModalContext] = useState(null)
  const { stack, visit } = useModalStack()
  const modalIndex = useModalIndex()

  const shouldNavigate = useMemo(() => {
    return navigate ?? getConfig('navigate')
  }, [navigate])

  // Separate standard props from custom event handlers
  const standardProps = {}
  const customEvents = {}

  Object.keys(props).forEach((key) => {
    if (modalPropNames.includes(key)) {
      return
    }

    if (key.startsWith('on') && typeof props[key] === 'function') {
      if (isStandardDomEvent(key)) {
        standardProps[key] = props[key]
      } else {
        customEvents[key] = props[key]
      }
    } else {
      standardProps[key] = props[key]
    }
  })

  const [isBlurred, setIsBlurred] = useState(false)

  useEffect(() => {
    if (!modalContext) {
      return
    }

    if (modalContext.onTopOfStack && isBlurred) {
      onFocus?.()
    } else if (!modalContext.onTopOfStack && !isBlurred) {
      onBlur?.()
    }

    setIsBlurred(!modalContext.onTopOfStack)
  }, [stack])

  const onCloseCallback = useCallback(() => {
    onClose?.()
  }, [onClose])

  const onAfterLeaveCallback = useCallback(() => {
    setModalContext(null)
    onAfterLeave?.()
  }, [onAfterLeave])

  const handle = useCallback(
    (e) => {
      e?.preventDefault()
      if (loading) return

      if (!href.startsWith('#')) {
        setLoading(true)
        onStart?.()
      }

      // In-modal navigation: replace content within the current modal
      if (replace && modalIndex !== null && stack[modalIndex]) {
        stack[modalIndex]
          .navigate(href, {
            method,
            data,
            headers,
            config: rejectNullValues(only(props, modalPropNames)),
            queryStringArrayFormat,
          })
          .then(() => {
            onSuccess?.()
          })
          .catch((error) => {
            console.error(error)
            onError?.(error)
          })
          .finally(() => setLoading(false))
        return
      }

      visit(
        href,
        method,
        data,
        headers,
        rejectNullValues(only(props, modalPropNames)),
        () => onCloseCallback(stack.length),
        onAfterLeaveCallback,
        queryStringArrayFormat,
        shouldNavigate,
      )
        .then((newModalContext) => {
          setModalContext(newModalContext)
          newModalContext.registerEventListenersFromProps(customEvents)
          onSuccess?.()
        })
        .catch((error) => {
          console.error(error)
          onError?.(error)
        })
        .finally(() => setLoading(false))
    },
    [href, method, data, headers, queryStringArrayFormat, props, onCloseCallback, onAfterLeaveCallback, replace, modalIndex],
  )

  return (
    <Component {...standardProps} href={href} onClick={handle}>
      {typeof children === 'function' ? children({ loading }) : children}
    </Component>
  )
}

export default ModalLink
