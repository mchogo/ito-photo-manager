"use client";

import { useEffect, useRef, useState } from "react";
import { usePathname, useRouter } from "next/navigation";
import Link from "next/link";
import { useAuth } from "@/lib/useAuth";
import { useNotifications } from "@/lib/useNotifications";

const WARN_PATHS = ["/shoot", "/preview"];
const WARN_MESSAGE =
  "作業を中断してトップに戻りますか？\n撮影した写真を含む現在のセッション内容はすべて失われます。";

const NOTIFICATION_COLORS: Record<string, string> = {
  入店リマインド: "rgba(99,102,241,",
  図書催促: "rgba(249,115,22,",
  再撮影指示: "rgba(239,68,68,",
};

export default function NavigationHeader() {
  const pathname = usePathname();
  const router = useRouter();
  const { user, isAdmin, logout } = useAuth();
  const { notifications, refresh } = useNotifications();
  const [panelOpen, setPanelOpen] = useState(false);
  const bellRef = useRef<HTMLDivElement>(null);

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

  const handleBellClick = () => {
    if (!panelOpen) refresh();
    setPanelOpen((v) => !v);
  };

  // クリック外で閉じる
  useEffect(() => {
    if (!panelOpen) return;
    const handleClickOutside = (e: MouseEvent) => {
      if (bellRef.current && !bellRef.current.contains(e.target as Node)) {
        setPanelOpen(false);
      }
    };
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, [panelOpen]);

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

      {/* Nav links + bell + user */}
      {!needsWarning && (
        <nav className="flex items-center gap-1">
          <NavLink href="/worker" active={pathname.startsWith("/worker")} label="ダッシュボード" icon="👷" />
          <NavLink href="/" active={pathname === "/"} label="新規案件" icon="＋" />
          {isAdmin && (
            <NavLink href="/admin" active={pathname.startsWith("/admin")} label="管理ボード" icon="🗂" />
          )}

          {/* Bell icon with notification panel */}
          <div ref={bellRef} className="relative ml-1">
            <button
              onClick={handleBellClick}
              className="relative flex items-center justify-center w-9 h-9 rounded-xl transition-all"
              style={{ border: "1px solid rgba(0,0,0,0.08)", color: "rgba(75,85,99,0.7)" }}
              aria-label="通知"
            >
              🔔
              {notifications.length > 0 && (
                <span
                  className="absolute -top-1 -right-1 min-w-[16px] h-4 rounded-full text-[10px] font-bold text-white flex items-center justify-center px-1"
                  style={{ background: "rgba(239,68,68,0.9)" }}
                >
                  {notifications.length}
                </span>
              )}
            </button>

            {panelOpen && (
              <div
                className="absolute right-0 top-full mt-2 w-72 rounded-2xl p-3 space-y-2 z-50"
                style={{
                  background: "rgba(255,255,255,0.88)",
                  backdropFilter: "blur(20px)",
                  border: "1px solid rgba(255,255,255,0.5)",
                  boxShadow: "0 8px 32px rgba(0,0,0,0.12)",
                }}
              >
                <p className="text-xs font-bold text-gray-700 px-1">
                  通知 {notifications.length > 0 ? `(${notifications.length})` : ""}
                </p>
                {notifications.length === 0 ? (
                  <p className="text-xs text-gray-400 text-center py-3">通知はありません</p>
                ) : (
                  notifications.map((n) => {
                    const c = NOTIFICATION_COLORS[n.type] ?? "rgba(75,85,99,";
                    return (
                      <Link
                        key={n.id}
                        href={`/projects/${n.projectId}`}
                        onClick={() => setPanelOpen(false)}
                        className="block rounded-xl p-2.5 text-xs font-medium transition-colors hover:bg-white/60"
                        style={{ border: "1px solid rgba(0,0,0,0.06)" }}
                      >
                        <span
                          className="text-[10px] font-bold px-2 py-0.5 rounded-full mr-2"
                          style={{ background: `${c}0.1)`, color: `${c}0.9)` }}
                        >
                          {n.type}
                        </span>
                        {n.message}
                      </Link>
                    );
                  })
                )}
              </div>
            )}
          </div>

          {/* User display + logout */}
          {user ? (
            <div className="flex items-center gap-1.5 ml-1">
              <div
                className="flex items-center gap-1.5 px-3 py-1.5 rounded-xl text-xs font-bold"
                style={{
                  background: isAdmin
                    ? "linear-gradient(135deg, rgba(234,88,12,0.12), rgba(249,115,22,0.12))"
                    : "rgba(0,0,0,0.04)",
                  border: isAdmin ? "1px solid rgba(234,88,12,0.25)" : "1px solid rgba(0,0,0,0.08)",
                  color: isAdmin ? "rgba(234,88,12,0.9)" : "rgba(75,85,99,0.8)",
                }}
              >
                <span>{isAdmin ? "🔑" : "👤"}</span>
                <span className="hidden sm:inline">{user.display_name}</span>
              </div>
              <button
                onClick={logout}
                className="flex items-center justify-center w-9 h-9 rounded-xl text-xs font-bold transition-all"
                style={{ border: "1px solid rgba(0,0,0,0.08)", color: "rgba(75,85,99,0.6)" }}
                aria-label="ログアウト"
                title="ログアウト"
              >
                🚪
              </button>
            </div>
          ) : (
            <Link
              href="/login"
              className="flex items-center gap-1.5 px-3 py-1.5 rounded-xl text-xs font-bold ml-1"
              style={{ border: "1px solid rgba(0,0,0,0.08)", color: "rgba(75,85,99,0.7)" }}
            >
              ログイン
            </Link>
          )}
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
