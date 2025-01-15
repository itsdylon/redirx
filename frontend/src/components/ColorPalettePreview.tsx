import React from 'react';

const ColorPalettePreview = () => {
  const palettes = [
    {
      name: "Charcoal & Electric",
      colors: {
        background: "#1a1a1a",     // Deep charcoal
        surface: "#242424",        // Lighter charcoal
        surfaceAlt: "#2d2d2d",    // Hover state
        text: "#ffffff",          // White text
        textSecondary: "#a3a3a3", // Muted text
        primary: "#00ffbb",       // Electric mint
        secondary: "#00cc96",     // Darker mint
        success: "#00f76c",       // Bright green
        warning: "#ffd60a",       // Bright yellow
        error: "#ff4d4d"          // Bright red
      }
    },
    {
      name: "Obsidian & Ice",
      colors: {
        background: "#121212",     // Nearly black
        surface: "#1e1e1e",        // Dark gray
        surfaceAlt: "#2a2a2a",    // Lighter gray
        text: "#ffffff",          // White text
        textSecondary: "#94a3b8", // Blue-gray text
        primary: "#60a5fa",       // Ice blue
        secondary: "#3b82f6",     // Darker blue
        success: "#4ade80",       // Soft green
        warning: "#fbbf24",       // Warm yellow
        error: "#f87171"          // Soft red
      }
    },
    {
      name: "Slate & Violet",
      colors: {
        background: "#1f1f1f",     // Deep slate
        surface: "#282828",        // Medium slate
        surfaceAlt: "#333333",    // Light slate
        text: "#ffffff",          // White text
        textSecondary: "#9ca3af", // Gray text
        primary: "#bd34fe",       // Bright violet
        secondary: "#9d00ff",     // Deep violet
        success: "#00e676",       // Material green
        warning: "#ffab00",       // Material amber
        error: "#ff5252"          // Material red
      }
    }
  ];

  return (
    <div className="p-6" style={{ background: '#0a0a0a' }}>
      <div className="max-w-6xl mx-auto space-y-12">
        <h1 className="text-2xl font-bold text-white mb-8">Dark Gray Color Palettes</h1>
        
        {palettes.map((palette) => (
          <div 
            key={palette.name}
            className="rounded-lg overflow-hidden"
            style={{ background: palette.colors.background }}
          >
            <div className="p-6 space-y-8">
              <h2 className="text-xl font-semibold" style={{ color: palette.colors.text }}>
                {palette.name}
              </h2>

              {/* Color Swatches */}
              <div className="grid grid-cols-2 md:grid-cols-5 gap-4">
                {Object.entries(palette.colors).map(([name, color]) => (
                  <div key={name} className="space-y-2">
                    <div 
                      className="h-20 rounded-lg shadow-lg"
                      style={{ backgroundColor: color }}
                    />
                    <div style={{ color: palette.colors.textSecondary }}>
                      <p className="font-medium capitalize">{name}</p>
                      <p className="font-mono text-sm">{color}</p>
                    </div>
                  </div>
                ))}
              </div>

              {/* Example UI */}
              <div 
                className="p-6 rounded-lg"
                style={{ backgroundColor: palette.colors.surface }}
              >
                <h3 className="text-lg font-medium mb-6" style={{ color: palette.colors.text }}>
                  Example UI Elements
                </h3>
                
                <div className="space-y-6">
                  {/* Content Card */}
                  <div 
                    className="p-4 rounded-lg"
                    style={{ backgroundColor: palette.colors.surfaceAlt }}
                  >
                    <div className="flex justify-between items-center mb-4">
                      <h4 style={{ color: palette.colors.text }}>Page Match</h4>
                      <span 
                        className="px-2 py-1 rounded text-sm"
                        style={{ 
                          backgroundColor: palette.colors.success,
                          color: palette.colors.background 
                        }}
                      >
                        98% Match
                      </span>
                    </div>
                    <div className="space-y-2">
                      <div style={{ color: palette.colors.textSecondary }}>
                        Original URL: /about-us
                      </div>
                      <div style={{ color: palette.colors.text }}>
                        New URL: /about-our-company
                      </div>
                    </div>
                  </div>

                  {/* Actions */}
                  <div className="flex gap-4">
                    <button 
                      className="px-4 py-2 rounded-md font-medium"
                      style={{ 
                        backgroundColor: palette.colors.primary,
                        color: palette.colors.background
                      }}
                    >
                      Generate Redirects
                    </button>
                    <button 
                      className="px-4 py-2 rounded-md font-medium"
                      style={{ 
                        backgroundColor: palette.colors.surface,
                        color: palette.colors.text,
                        border: `1px solid ${palette.colors.surfaceAlt}`
                      }}
                    >
                      Cancel
                    </button>
                  </div>

                  {/* Status Indicators */}
                  <div className="flex gap-3">
                    {[
                      { label: 'Exact Match', color: palette.colors.success },
                      { label: 'Partial Match', color: palette.colors.warning },
                      { label: 'Low Match', color: palette.colors.error }
                    ].map(status => (
                      <div 
                        key={status.label}
                        className="px-3 py-1 rounded-full text-sm"
                        style={{ 
                          backgroundColor: status.color,
                          color: palette.colors.background
                        }}
                      >
                        {status.label}
                      </div>
                    ))}
                  </div>

                  {/* Progress Bar */}
                  <div>
                    <div className="h-2 rounded-full" style={{ backgroundColor: palette.colors.surface }}>
                      <div 
                        className="h-2 rounded-full w-2/3 transition-all duration-300"
                        style={{ backgroundColor: palette.colors.primary }}
                      />
                    </div>
                  </div>
                </div>
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
};

export default ColorPalettePreview;