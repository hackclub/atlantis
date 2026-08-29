import { Dialog, Transition, TransitionChild } from '@headlessui/react'
import { createContext, useContext, forwardRef, useRef, useImperativeHandle } from 'react'
import HeadlessModal from './HeadlessModal'
import CloseButton from './CloseButton'
import ModalContent from './ModalContent'
import SlideoverContent from './SlideoverContent'

// When a Modal renders navigated content, child Modals rendered by the
// NavigatedComponent should skip Dialog/Transition/backdrop but still
// apply their own config (panelClasses, maxWidth, etc.).
// This lets pages use the same <Modal> markup in standalone and navigated contexts.
const NavigatedModalContext = createContext(null)

const Modal = forwardRef(
  (
    { name, children, onFocus = null, onBlur = null, onClose = null, onSuccess = null, onAfterLeave = null, ...props },
    ref,
  ) => {
    const navigatedParent = useContext(NavigatedModalContext)

    const renderChildren = (contentProps) => {
      if (typeof children === 'function') {
        return children(contentProps)
      }

      return children
    }

    // When this Modal is rendered inside navigated content, render a simple
    // wrapper that applies panelClasses/paddingClasses without duplicating
    // the parent's Dialog/Transition/fixed-overlay infrastructure.
    if (navigatedParent) {
      const childConfig = {
        slideover: props.slideover ?? false,
        closeButton: props.closeButton ?? false,
        closeExplicitly: props.closeExplicitly ?? false,
        maxWidth: props.maxWidth ?? navigatedParent.config.maxWidth,
        paddingClasses: props.paddingClasses ?? navigatedParent.config.paddingClasses,
        panelClasses: props.panelClasses ?? navigatedParent.config.panelClasses,
        position: props.position ?? navigatedParent.config.position,
        duration: props.duration ?? navigatedParent.config.duration ?? 300,
      }

      const content = renderChildren({
        close: navigatedParent.close,
        navigate: navigatedParent.navigate,
        goBack: navigatedParent.goBack,
        canGoBack: navigatedParent.canGoBack,
        config: childConfig,
        modalContext: navigatedParent.modalContext,
      })

      return (
        <div className={`im-modal-content relative ${childConfig.paddingClasses ?? ''} ${childConfig.panelClasses ?? ''}`}>
          {childConfig.closeButton && (
            <div className="absolute right-0 top-0 pr-3 pt-3">
              <CloseButton onClick={navigatedParent.close} />
            </div>
          )}
          {content}
        </div>
      )
    }

    const headlessModalRef = useRef(null)

    useImperativeHandle(ref, () => headlessModalRef.current, [headlessModalRef])

    return (
      <HeadlessModal
        ref={headlessModalRef}
        name={name}
        onFocus={onFocus}
        onBlur={onBlur}
        onClose={onClose}
        onSuccess={onSuccess}
        {...props}
      >
        {({
          afterLeave,
          close,
          config,
          emit,
          getChildModal,
          getParentModal,
          id,
          index,
          isOpen,
          modalContext,
          onTopOfStack,
          reload,
          setOpen,
          shouldRender,
          navigate,
          goBack,
          canGoBack,
        }) => {
          const NavigatedComponent = modalContext.navigatedContent?.component
          const navigatedConfig = modalContext.navigatedContent?.config
            ? { ...config, ...modalContext.navigatedContent.config }
            : config

          const childProps = {
            afterLeave,
            close,
            config: navigatedConfig,
            emit,
            getChildModal,
            getParentModal,
            id,
            index,
            isOpen,
            modalContext,
            onTopOfStack,
            reload,
            setOpen,
            shouldRender,
            navigate,
            goBack,
            canGoBack,
          }

          const innerContent = NavigatedComponent ? (
            <NavigatedModalContext.Provider value={{ modalContext, config: navigatedConfig, close, navigate, goBack, canGoBack }}>
              <NavigatedComponent
                {...modalContext.navigatedContent.props}
                close={close}
                navigate={navigate}
                goBack={goBack}
                canGoBack={canGoBack}
              />
            </NavigatedModalContext.Provider>
          ) : (
            renderChildren(childProps)
          )

          return (
            <Transition appear={true} show={isOpen ?? false} afterLeave={onAfterLeave}>
              <Dialog
                as="div"
                className="im-dialog relative z-20"
                onClose={() => (config.closeExplicitly ? null : close())}
                data-inertiaui-modal-id={id}
                data-inertiaui-modal-index={index}
              >
                {/* Only transition the backdrop for the first modal in the stack */}
                {index === 0 ? (
                  <>
                    {config.duration !== 300 && (
                      <style>{`.im-bdur-${id.replace(/[^a-zA-Z0-9]/g, '')} { transition-duration: ${config.duration}ms !important; }`}</style>
                    )}
                    <TransitionChild
                      enter={`transition transform ease-in-out duration-300 ${config.duration !== 300 ? `im-bdur-${id.replace(/[^a-zA-Z0-9]/g, '')}` : ''}`}
                      enterFrom={config.duration === 0 ? '' : 'opacity-0'}
                      enterTo="opacity-100"
                      leave={`transition transform ease-in-out duration-300 ${config.duration !== 300 ? `im-bdur-${id.replace(/[^a-zA-Z0-9]/g, '')}` : ''}`}
                      leaveFrom="opacity-100"
                      leaveTo={config.duration === 0 ? '' : 'opacity-0'}
                    >
                      {onTopOfStack ? (
                        <div className="im-backdrop fixed inset-0 z-30 bg-black/75" aria-hidden="true" />
                      ) : (
                        <div />
                      )}
                    </TransitionChild>
                  </>
                ) : null}

                {/* On multiple modals, only show a backdrop for the modal that is on top of the stack */}
                {index > 0 && onTopOfStack ? <div className="im-backdrop fixed inset-0 z-30 bg-black/75" /> : null}

                {/* The modal/slideover content itself */}
                {config.slideover ? (
                  <SlideoverContent modalContext={modalContext} config={navigatedConfig}>
                    {innerContent}
                  </SlideoverContent>
                ) : (
                  <ModalContent modalContext={modalContext} config={navigatedConfig}>
                    {innerContent}
                  </ModalContent>
                )}
              </Dialog>
            </Transition>
          )
        }}
      </HeadlessModal>
    )
  },
)

Modal.displayName = 'Modal'
export default Modal
