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
        // Bitnade palette: monochrome neutrals, former accent hues mapped to the light end.
        slate: {"50": "#fafafa", "100": "#ededed", "200": "#d4d4d4", "300": "#bababa", "400": "#a1a1a1", "500": "#737373", "600": "#525252", "700": "#2a2a2a", "800": "#1c1c1c", "900": "#141414", "950": "#0a0a0a"},
        gray: {"50": "#fafafa", "100": "#ededed", "200": "#d4d4d4", "300": "#bababa", "400": "#a1a1a1", "500": "#737373", "600": "#525252", "700": "#2a2a2a", "800": "#1c1c1c", "900": "#141414", "950": "#0a0a0a"},
        indigo: {"50": "#fafafa", "100": "#f5f5f5", "200": "#ededed", "300": "#ededed", "400": "#d4d4d4", "500": "#bdbdbd", "600": "#ededed", "700": "#d4d4d4", "800": "#2a2a2a", "900": "#1c1c1c", "950": "#141414"},
        purple: {"50": "#fafafa", "100": "#f5f5f5", "200": "#ededed", "300": "#ededed", "400": "#d4d4d4", "500": "#bdbdbd", "600": "#ededed", "700": "#d4d4d4", "800": "#2a2a2a", "900": "#1c1c1c", "950": "#141414"},
        violet: {"50": "#fafafa", "100": "#f5f5f5", "200": "#ededed", "300": "#ededed", "400": "#d4d4d4", "500": "#bdbdbd", "600": "#ededed", "700": "#d4d4d4", "800": "#2a2a2a", "900": "#1c1c1c", "950": "#141414"},
        blue: {"50": "#fafafa", "100": "#f5f5f5", "200": "#ededed", "300": "#ededed", "400": "#d4d4d4", "500": "#bdbdbd", "600": "#ededed", "700": "#d4d4d4", "800": "#2a2a2a", "900": "#1c1c1c", "950": "#141414"},
        cyan: {"50": "#fafafa", "100": "#f5f5f5", "200": "#ededed", "300": "#ededed", "400": "#d4d4d4", "500": "#bdbdbd", "600": "#ededed", "700": "#d4d4d4", "800": "#2a2a2a", "900": "#1c1c1c", "950": "#141414"},
        sky: {"50": "#fafafa", "100": "#f5f5f5", "200": "#ededed", "300": "#ededed", "400": "#d4d4d4", "500": "#bdbdbd", "600": "#ededed", "700": "#d4d4d4", "800": "#2a2a2a", "900": "#1c1c1c", "950": "#141414"},
        pink: {"50": "#fafafa", "100": "#f5f5f5", "200": "#ededed", "300": "#ededed", "400": "#d4d4d4", "500": "#bdbdbd", "600": "#ededed", "700": "#d4d4d4", "800": "#2a2a2a", "900": "#1c1c1c", "950": "#141414"},
        fuchsia: {"50": "#fafafa", "100": "#f5f5f5", "200": "#ededed", "300": "#ededed", "400": "#d4d4d4", "500": "#bdbdbd", "600": "#ededed", "700": "#d4d4d4", "800": "#2a2a2a", "900": "#1c1c1c", "950": "#141414"},
        teal: {"50": "#fafafa", "100": "#f5f5f5", "200": "#ededed", "300": "#ededed", "400": "#d4d4d4", "500": "#bdbdbd", "600": "#ededed", "700": "#d4d4d4", "800": "#2a2a2a", "900": "#1c1c1c", "950": "#141414"},
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
