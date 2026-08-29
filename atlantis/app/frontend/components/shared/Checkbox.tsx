interface CheckboxProps {
  checked: boolean
  onChange: (checked: boolean) => void
  label: string
}

export default function Checkbox({ checked, onChange, label }: CheckboxProps) {
  return (
    <label className="flex items-center gap-3 cursor-pointer select-none">
      <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} className="hidden" />
      <span
        className={`shrink-0 w-5 h-5 rounded border-2 grid place-content-center ${
          checked ? 'bg-brown border-dark-brown' : 'bg-white border-dark-brown/30'
        }`}
      >
        {checked && (
          <svg
            className="w-3 h-3 text-light-brown"
            viewBox="0 0 12 12"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            <path d="M2 6l3 3 5-5" />
          </svg>
        )}
      </span>
      <span className="text-lg font-bold">{label}</span>
    </label>
  )
}
