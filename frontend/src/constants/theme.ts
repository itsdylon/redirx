export const THEME = {
    colors: {
      background: '#1a1a1a',
      surface: '#242424',
      surfaceHover: '#2d2d2d',
      primary: '#00ffbb',
      primaryHover: '#00cc96',
      text: '#ffffff',
      textSecondary: '#a3a3a3',
      success: '#00f76c',
      warning: '#ffd60a',
      error: '#ff4d4d'
    },
    
    spacing: {
      xs: '0.25rem',    // 4px
      sm: '0.5rem',     // 8px
      md: '1rem',       // 16px
      lg: '1.5rem',     // 24px
      xl: '2rem',       // 32px
      '2xl': '2.5rem',  // 40px
      '3xl': '3rem'     // 48px
    },
    
    borderRadius: {
      sm: '0.25rem',    // 4px
      md: '0.375rem',   // 6px
      lg: '0.5rem',     // 8px
      full: '9999px'
    },
    
    typography: {
      fontFamily: {
        sans: 'Inter, system-ui, -apple-system, sans-serif',
        mono: 'JetBrains Mono, monospace'
      },
      fontSize: {
        xs: '0.75rem',    // 12px
        sm: '0.875rem',   // 14px
        base: '1rem',     // 16px
        lg: '1.125rem',   // 18px
        xl: '1.25rem',    // 20px
        '2xl': '1.5rem',  // 24px
        '3xl': '1.875rem' // 30px
      },
      fontWeight: {
        normal: 400,
        medium: 500,
        semibold: 600,
        bold: 700
      }
    },
    
    shadows: {
      sm: '0 1px 2px rgba(0, 0, 0, 0.4)',
      md: '0 4px 6px rgba(0, 0, 0, 0.4)',
      lg: '0 10px 15px rgba(0, 0, 0, 0.4)'
    },
    
    transitions: {
      fast: '150ms ease',
      normal: '200ms ease',
      slow: '300ms ease'
    }
  } as const;