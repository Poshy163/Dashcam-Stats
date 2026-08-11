/** @type {import('tailwindcss').Config} */
export default {
  darkMode: 'class',
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        // Semantic tokens rather than raw palette values, so the whole UI can be
        // retinted from one place and light/dark stay in step.
        surface: {
          DEFAULT: 'rgb(var(--surface) / <alpha-value>)',
          raised: 'rgb(var(--surface-raised) / <alpha-value>)',
          sunken: 'rgb(var(--surface-sunken) / <alpha-value>)',
        },
        border: 'rgb(var(--border) / <alpha-value>)',
        content: {
          DEFAULT: 'rgb(var(--content) / <alpha-value>)',
          muted: 'rgb(var(--content-muted) / <alpha-value>)',
          faint: 'rgb(var(--content-faint) / <alpha-value>)',
        },
        accent: {
          DEFAULT: 'rgb(var(--accent) / <alpha-value>)',
          muted: 'rgb(var(--accent-muted) / <alpha-value>)',
        },
        nav: {
          DEFAULT: 'rgb(var(--nav) / <alpha-value>)',
          raised: 'rgb(var(--nav-raised) / <alpha-value>)',
          border: 'rgb(var(--nav-border) / <alpha-value>)',
          content: 'rgb(var(--nav-content) / <alpha-value>)',
          muted: 'rgb(var(--nav-muted) / <alpha-value>)',
        },
        // Processing states get fixed meanings across every page: a failed job looks
        // the same in the queue, the recordings grid and the dashboard.
        state: {
          ok: 'rgb(var(--state-ok) / <alpha-value>)',
          warn: 'rgb(var(--state-warn) / <alpha-value>)',
          error: 'rgb(var(--state-error) / <alpha-value>)',
          busy: 'rgb(var(--state-busy) / <alpha-value>)',
          idle: 'rgb(var(--state-idle) / <alpha-value>)',
        },
      },
      fontFamily: {
        sans: ['Inter var', 'Inter', 'system-ui', '-apple-system', 'Segoe UI', 'sans-serif'],
        mono: ['ui-monospace', 'SFMono-Regular', 'Menlo', 'Consolas', 'monospace'],
      },
      fontSize: {
        '2xs': ['0.6875rem', { lineHeight: '1rem' }],
      },
      boxShadow: {
        card: '0 1px 2px rgb(15 23 42 / 0.04), 0 8px 24px rgb(15 23 42 / 0.04)',
        float: '0 16px 48px rgb(15 23 42 / 0.16)',
      },
    },
  },
  plugins: [],
}
