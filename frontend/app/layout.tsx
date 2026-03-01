import type { Metadata } from "next";
import "./globals.css";
import NavigationHeader from "@/components/NavigationHeader";

export const metadata: Metadata = {
  title: "いとうさんフォトマネージャー",
  description: "現場撮影管理・Excel自動化システム",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="ja">
      <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
      <body className="min-h-screen">
        {/* === Floating pastel blobs (iOS background) === */}
        <div className="bg-blob bg-blob-1" />
        <div className="bg-blob bg-blob-2" />
        <div className="bg-blob bg-blob-3" />

        {/* === Glass Header === */}
        <header className="sticky top-0 z-50 liquid-glass" style={{
          borderRadius: 0,
          borderTop: "none",
          borderLeft: "none",
          borderRight: "none",
          background: "rgba(255,255,255,0.35)",
          paddingTop: "env(safe-area-inset-top)",
        }}>
          <div className="max-w-4xl mx-auto px-5 py-3 flex items-center gap-3">
            <NavigationHeader />
          </div>
        </header>

        <main className="max-w-4xl mx-auto px-5 py-6 relative z-10">
          {children}
        </main>
      </body>
    </html>
  );
}
