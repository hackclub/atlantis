import { twMerge } from 'tailwind-merge'

type TextAreaProps = React.TextareaHTMLAttributes<HTMLTextAreaElement>

export default function TextArea({ className, ...props }: TextAreaProps) {
  return (
    <textarea
      className={twMerge('w-full border-2 border-dark-brown rounded px-4 py-3 bg-white resize-none', className)}
      {...props}
    />
  )
}
