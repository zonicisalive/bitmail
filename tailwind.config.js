/** @type {import('tailwindcss').Config} */
module.exports = {
  darkMode: 'class',
  content: [
    "./app/templates/**/*.{html,js}",
    "./app/static/**/*.{html,js}",
  ],
  safelist: [
    'hidden',
    'active',
    'opacity-0',
    'opacity-100',
    'translate-x-4',
    'translate-y-2',
    'translate-y-0',
    'bg-indigo-600',
    'bg-emerald-600',
    'bg-rose-600',
    'bg-red-600',
    'text-white',
    'text-slate-300',
    'text-slate-400',
    'text-indigo-400',
    'border-b-2',
    'border-indigo-500',
    'border-emerald-500',
    'border-white/10',
    'bg-white/10',
    'bg-white/5',
  ],
  theme: {
    extend: {
      colors: {
        brand: {
          50: '#eef2ff',
          100: '#e0e7ff',
          400: '#818cf8',
          500: '#6366f1',
          600: '#4f46e5',
          700: '#4338ca',
        },
        dark: {
          bg: '#0b0f19',
          surface: '#111827',
          card: '#1f2937',
          border: 'rgba(255, 255, 255, 0.08)'
        }
      },
      fontFamily: {
        sans: ['"Plus Jakarta Sans"', 'sans-serif'],
        mono: ['"JetBrains Mono"', 'monospace']
      }
    }
  },
  plugins: [],
}
