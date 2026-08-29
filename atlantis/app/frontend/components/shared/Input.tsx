import { twMerge } from 'tailwind-merge'

type InputProps = React.InputHTMLAttributes<HTMLInputElement>

export default function Input({ className, ...props }: InputProps) {
  return (
    <input className={twMerge('w-full border-2 border-dark-brown rounded px-4 py-3 bg-white', className)} {...props} />
  )
}
