import { useState } from 'react'

export function RefreshButton({
  onClick,
  title = 'Refresh',
}: {
  onClick: () => void
  title?: string
}) {
  const [spinning, setSpinning] = useState(false)

  function handleClick() {
    onClick()
    setSpinning(true)
    setTimeout(() => setSpinning(false), 500)
  }

  return (
    <button
      type="button"
      onClick={handleClick}
      title={title}
      aria-label={title}
      className="rounded border border-neutral-700 p-1 text-neutral-500 hover:text-neutral-200"
    >
      <svg
        viewBox="0 0 24 24"
        width="12"
        height="12"
        fill="none"
        stroke="currentColor"
        strokeWidth="2.5"
        strokeLinecap="round"
        strokeLinejoin="round"
        className={spinning ? 'animate-spin' : ''}
      >
        <path d="M21 12a9 9 0 1 1-2.64-6.36" />
        <path d="M21 3v6h-6" />
      </svg>
    </button>
  )
}
