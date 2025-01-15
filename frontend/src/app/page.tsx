import ColorPalettePreview from '@/components/ColorPalettePreview'
import URLComparisonTool from '@/components/URLComparisonTool'

export default function Home() {
  return (
    <main className="min-h-screen p-4">
      <URLComparisonTool />
      <ColorPalettePreview />
    </main>
  )
}