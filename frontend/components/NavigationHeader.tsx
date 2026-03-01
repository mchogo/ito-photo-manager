"use client";

import { usePathname, useRouter } from "next/navigation";
import Link from "next/link";

const WARN_PATHS = ["/shoot", "/preview"];
const WARN_MESSAGE =
  "作業を中断してトップに戻りますか？\n撮影した写真を含む現在のセッション内容はすべて失われます。";

export default function NavigationHeader() {
  const pathname = usePathname();
  const router = useRouter();

  const needsWarning = WARN_PATHS.some((p) => pathname.startsWith(p));

  const handleLogoClick = (e: React.MouseEvent) => {
    if (pathname === "/") return;
    if (needsWarning) {
      e.preventDefault();
      if (confirm(WARN_MESSAGE)) {
        router.push("/");
      }
    }
  };

  return (
    <div className="flex items-center justify-between w-full">
      {/* Logo */}
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

      {/* Nav links */}
      {!needsWarning && (
        <nav className="flex items-center gap-1">
          <NavLink href="/worker" active={pathname.startsWith("/worker")} label="ダッシュボード" icon="👷" />
          <NavLink href="/" active={pathname === "/"} label="新規案件" icon="＋" />
        </nav>
      )}
    </div>
  );
}

function NavLink({
  href,
  active,
  label,
  icon,
}: {
  href: string;
  active: boolean;
  label: string;
  icon: string;
}) {
  return (
    <Link
      href={href}
      className="flex items-center gap-1.5 px-3 py-1.5 rounded-xl text-xs font-bold transition-all"
      style={
        active
          ? {
              background: "linear-gradient(135deg, rgba(99,102,241,0.15), rgba(139,92,246,0.15))",
              color: "rgba(99,102,241,0.9)",
              border: "1px solid rgba(99,102,241,0.2)",
            }
          : {
              color: "rgba(75,85,99,0.7)",
            }
      }
    >
      <span>{icon}</span>
      <span className="hidden sm:inline">{label}</span>
    </Link>
  );
}
