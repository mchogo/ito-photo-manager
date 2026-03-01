"use client";

import { usePathname, useRouter } from "next/navigation";

const WARN_PATHS = ["/shoot", "/preview"];
const WARN_MESSAGE =
  "作業を中断してトップに戻りますか？\n撮影した写真を含む現在のセッション内容はすべて失われます。";

export default function NavigationHeader() {
  const pathname = usePathname();
  const router = useRouter();

  const needsWarning = WARN_PATHS.some((p) => pathname.startsWith(p));

  const handleLogoClick = (e: React.MouseEvent) => {
    if (pathname === "/") return; // already on top
    if (needsWarning) {
      e.preventDefault();
      if (confirm(WARN_MESSAGE)) {
        router.push("/");
      }
    }
    // if not on a warn path, let the Link navigate normally
  };

  return (
    <a href="/" onClick={handleLogoClick} className="flex items-center gap-3 tap-target">
      <div
        className="w-10 h-10 rounded-2xl flex items-center justify-center text-xl"
        style={{
          background: "linear-gradient(135deg, rgba(99,102,241,0.2), rgba(139,92,246,0.2))",
          border: "1px solid rgba(255,255,255,0.5)",
          boxShadow: "inset 0 1px 0 rgba(255,255,255,0.6)",
        }}
      >
        📷
      </div>
      <div>
        <h1 className="text-[15px] font-extrabold text-gray-900 tracking-tight">
          フォトマネージャー
        </h1>
        <p className="text-[11px] text-gray-600/80 -mt-0.5 font-semibold">
          現場撮影管理システム
        </p>
      </div>
    </a>
  );
}
