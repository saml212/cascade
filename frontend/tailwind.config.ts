import type { Config } from 'tailwindcss';

export default {
  content: ['./index.html', './src/**/*.{ts,tsx,html}'],
  theme: {
    extend: {
      colors: {
        canvas: 'var(--surface-canvas)',
        surface: {
          1: 'var(--surface-1)',
          2: 'var(--surface-2)',
          3: 'var(--surface-3)',
          inset: 'var(--surface-inset)',
        },
        border: {
          subtle: 'var(--border-subtle)',
          DEFAULT: 'var(--border)',
          strong: 'var(--border-strong)',
        },
        ink: {
          primary: 'var(--text-primary)',
          secondary: 'var(--text-secondary)',
          tertiary: 'var(--text-tertiary)',
          disabled: 'var(--text-disabled)',
          'on-accent': 'var(--text-on-accent)',
        },
        accent: {
          DEFAULT: 'var(--accent)',
          soft: 'var(--accent-soft)',
        },
        status: {
          success: 'var(--status-success)',
          working: 'var(--status-working)',
          warning: 'var(--status-warning)',
          danger: 'var(--status-danger)',
          neutral: 'var(--status-neutral)',
        },
        speaker: {
          1: 'var(--speaker-1)',
          2: 'var(--speaker-2)',
          3: 'var(--speaker-3)',
          4: 'var(--speaker-4)',
        },
      },
      fontFamily: {
        display: ['"Bricolage Grotesque"', 'ui-sans-serif', 'system-ui', 'sans-serif'],
        sans: ['Satoshi', 'ui-sans-serif', 'system-ui', 'sans-serif'],
        mono: ['"JetBrains Mono"', 'ui-monospace', 'SFMono-Regular', 'Menlo', 'monospace'],
      },
      fontSize: {
        'display-xl': ['2.5rem', { lineHeight: '2.75rem', fontWeight: '600', letterSpacing: '-0.02em' }],
        'display-lg': ['1.75rem', { lineHeight: '2.125rem', fontWeight: '600', letterSpacing: '-0.015em' }],
        'display-md': ['1.375rem', { lineHeight: '1.75rem', fontWeight: '600', letterSpacing: '-0.01em' }],
        'heading-lg': ['1.25rem', { lineHeight: '1.75rem', fontWeight: '600' }],
        'heading-md': ['1rem', { lineHeight: '1.5rem', fontWeight: '600' }],
        'heading-sm': ['0.8125rem', { lineHeight: '1.125rem', fontWeight: '600', letterSpacing: '0.04em' }],
        body: ['0.875rem', { lineHeight: '1.375rem', fontWeight: '400' }],
        'body-lg': ['1rem', { lineHeight: '1.625rem', fontWeight: '400' }],
        'body-sm': ['0.8125rem', { lineHeight: '1.25rem', fontWeight: '400' }],
        code: ['0.8125rem', { lineHeight: '1.125rem', fontWeight: '500' }],
        'code-sm': ['0.6875rem', { lineHeight: '1rem', fontWeight: '500' }],
      },
      borderRadius: {
        sm: '4px',
        DEFAULT: '6px',
        md: '8px',
        lg: '12px',
      },
      boxShadow: {
        'lift-sm': '0 1px 0 rgba(255, 240, 220, 0.04) inset, 0 1px 2px rgba(0,0,0,0.4)',
        'lift-md': '0 1px 0 rgba(255, 240, 220, 0.05) inset, 0 4px 16px rgba(0,0,0,0.4)',
        'lift-lg': '0 1px 0 rgba(255, 240, 220, 0.06) inset, 0 12px 40px rgba(0,0,0,0.5)',
      },
      transitionTimingFunction: {
        expressive: 'cubic-bezier(0.2, 0.8, 0.2, 1)',
      },
      animation: {
        'pulse-breath': 'pulseBreath 2s ease-in-out infinite',
        'scanline': 'scanline 1.8s linear infinite',
        'fade-up': 'fadeUp 240ms cubic-bezier(0.2, 0.8, 0.2, 1) both',
      },
      keyframes: {
        pulseBreath: {
          '0%, 100%': { opacity: '0.6', transform: 'scale(1)' },
          '50%': { opacity: '1', transform: 'scale(1.08)' },
        },
        scanline: {
          '0%': { transform: 'translateX(-100%)' },
          '100%': { transform: 'translateX(100%)' },
        },
        fadeUp: {
          '0%': { opacity: '0', transform: 'translateY(4px)' },
          '100%': { opacity: '1', transform: 'translateY(0)' },
        },
      },
    },
  },
  plugins: [],
} satisfies Config;
