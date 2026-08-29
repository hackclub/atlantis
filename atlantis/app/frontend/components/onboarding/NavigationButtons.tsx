import TextMorph from '@/components/shared/TextMorph'

interface NavigationButtonsProps {
  backVisible?: boolean
  backDisabled?: boolean
  onBack?: () => void
  continueVisible?: boolean
  continueDisabled?: boolean
  continueLabel: string
  onContinue?: () => void
  continueType?: 'button' | 'submit'
  continueTransitionOut?: boolean
  sceneTransitionEase?: string
  layout?: 'absolute' | 'footer'
}

export default function NavigationButtons({
  backVisible = false,
  backDisabled = false,
  onBack,
  continueVisible = false,
  continueDisabled = false,
  continueLabel,
  onContinue,
  continueType = 'button',
  continueTransitionOut = false,
  sceneTransitionEase = 'cubic-bezier(0.22, 1, 0.36, 1)',
  layout = 'absolute',
}: NavigationButtonsProps) {
  const backInteractive = backVisible && !backDisabled
  const continueInteractive = continueVisible && !continueDisabled
  const footerLayout = layout === 'footer'

  const backButton = (
    <button
      type="button"
      className={`flex items-center gap-3 text-2xl lg:text-3xl font-bold text-dark-brown transform-gpu transition-all duration-200 ${
        footerLayout ? 'self-start' : 'z-20 absolute bottom-6 left-6 lg:bottom-10 lg:left-10'
      } ${backVisible ? 'opacity-100 translate-y-0' : 'opacity-0 translate-y-2 pointer-events-none'} ${
        backInteractive ? 'cursor-pointer hover:text-brown hover:scale-[1.02] focus:scale-100 active:scale-100' : ''
      } ${backVisible && backDisabled ? 'opacity-50 cursor-not-allowed' : ''}`}
      onClick={onBack}
      disabled={!backVisible || backDisabled}
    >
      <svg
        xmlns="http://www.w3.org/2000/svg"
        viewBox="0 0 20 20"
        fill="currentColor"
        className="w-8 h-8 lg:w-10 lg:h-10"
      >
        <path
          fillRule="evenodd"
          d="M17 10a.75.75 0 0 1-.75.75H5.612l4.158 3.96a.75.75 0 1 1-1.04 1.08l-5.5-5.25a.75.75 0 0 1 0-1.08l5.5-5.25a.75.75 0 1 1 1.04 1.08L5.612 9.25H16.25A.75.75 0 0 1 17 10Z"
          clipRule="evenodd"
        />
      </svg>
      Go back
    </button>
  )

  const continueButton = (
    <div
      className={`transform-gpu transition-all duration-200 ${
        footerLayout
          ? `self-stretch sm:self-auto ${continueVisible ? 'opacity-100 translate-y-0' : 'opacity-0 translate-y-2 pointer-events-none'} sm:ml-auto`
          : `z-20 absolute bottom-6 right-6 lg:bottom-10 lg:right-10 ${continueVisible ? 'opacity-100 translate-y-0' : 'opacity-0 translate-y-2 pointer-events-none'}`
      }`}
      style={{
        opacity: continueTransitionOut ? 0 : undefined,
        transform: continueTransitionOut ? 'translateY(28px) scale(0.95)' : undefined,
        filter: continueTransitionOut ? 'blur(4px)' : undefined,
        transition: continueTransitionOut
          ? `opacity 480ms ${sceneTransitionEase} 260ms, transform 680ms ${sceneTransitionEase} 260ms, filter 480ms ${sceneTransitionEase} 260ms`
          : undefined,
      }}
    >
      <div className={continueInteractive ? 'continue-button-breathe' : footerLayout ? 'block' : 'inline-block'}>
        <button
          type={continueType}
          className={`inline-flex min-w-44 items-center justify-center py-4 px-10 lg:min-w-52 lg:py-5 lg:px-14 bg-dark-brown text-light-brown rounded-2xl font-bold text-xl lg:text-2xl border-dark-brown border-2 transform-gpu transition-all duration-200 ${
            footerLayout ? 'w-full sm:w-auto' : ''
          } ${
            continueInteractive
              ? 'cursor-pointer hover:bg-light-brown hover:text-dark-brown hover:scale-[1.02] focus:scale-100 active:scale-100'
              : ''
          } ${continueVisible && continueDisabled ? 'opacity-70 cursor-not-allowed' : ''}`}
          onClick={continueType === 'button' ? onContinue : undefined}
          disabled={!continueVisible || continueDisabled}
        >
          <TextMorph as="span" className="inline-block">
            {continueLabel}
          </TextMorph>
        </button>
      </div>
    </div>
  )

  if (footerLayout) {
    return (
      <div className="z-20 flex w-full flex-col gap-3 sm:flex-row sm:items-end">
        {backVisible ? backButton : null}
        {continueVisible || continueTransitionOut ? continueButton : null}
      </div>
    )
  }

  return (
    <>
      {backButton}
      {continueButton}
    </>
  )
}
