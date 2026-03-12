"use client";

import { useEffect, useRef, useState } from "react";
import { usePathname, useRouter } from "next/navigation";
import Link from "next/link";
import { useAuth } from "@/lib/useAuth";
import { useTheme } from "@/lib/ThemeContext";
import { useNotifications } from "@/lib/useNotifications";

const WARN_PATHS = ["/shoot", "/preview"];
const WARN_MESSAGE =
  "作業を中断してトップに戻りますか？現在のセッション内容は失われます（提出した写真は保存されています）";

const NOTIFICATION_COLORS: Record<string, string> = {
  入店リマインド: "rgba(129,140,248,",
  図書催促:       "rgba(251,146,60,",
  再撮影指示:     "rgba(248,113,113,",
};

export default function NavigationHeader() {
  const pathname = usePathname();
  const router = useRouter();
  const { user, isAdmin, logout } = useAuth();
  const { theme, toggleTheme } = useTheme();
  const { notifications, refresh } = useNotifications();
  const [panelOpen, setPanelOpen] = useState(false);
  const bellRef = useRef<HTMLDivElement>(null);

  const needsWarning = WARN_PATHS.some((p) => pathname.startsWith(p));

  const handleLogoClick = (e: React.MouseEvent) => {
    if (pathname === "/") return;
    if (needsWarning) {
      e.preventDefault();
      if (confirm(WARN_MESSAGE)) router.push("/");
    }
  };

  const handleBellClick = () => {
    if (!panelOpen) refresh();
    setPanelOpen((v) => !v);
  };

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
      <Link href="/" onClick={handleLogoClick} className="flex items-center gap-3 tap-target">
        <div
          className="w-10 h-10 rounded-2xl flex items-center justify-center text-xl"
          style={{
            background: "var(--c-logo-bg)",
            border: "1px solid var(--c-logo-border)",
            boxShadow: "var(--c-logo-shadow)",
          }}
        >
          📷
        </div>
        <div>
          <h1
            className="text-[15px] font-extrabold tracking-tight"
            style={{ color: "var(--c-logo-title)" }}
          >
            フォトマネージャー
          </h1>
          <p
            className="text-[11px] -mt-0.5 font-semibold"
            style={{ color: "var(--c-logo-sub)" }}
          >
            現場撮影管理システム
          </p>
        </div>
      </Link>

      {/* Nav links + bell + user */}
      {!needsWarning && (
        <nav className="flex items-center gap-1">
          <NavLink href="/worker" active={pathname.startsWith("/worker")} label="ダッシュボード" icon="👷" />
          <NavLink href="/"       active={pathname === "/"}               label="新規案件"       icon="＋" />
          {isAdmin && (
            <NavLink href="/admin" active={pathname.startsWith("/admin")} label="管理ボード" icon="🗂" />
          )}

          {/* Theme toggle */}
          <button
            onClick={toggleTheme}
            className="flex items-center justify-center w-9 h-9 rounded-xl text-base transition-all"
            style={{ border: "1px solid var(--c-icon-btn-border)", color: "var(--c-icon-btn-color)" }}
            aria-label={theme === "dark" ? "ライトモードに切り替え" : "ダークモードに切り替え"}
            title={theme === "dark" ? "ライトモード" : "ダークモード"}
          >
            {theme === "dark" ? "☀️" : "🌙"}
          </button>

          {/* Bell icon with notification panel */}
          <div ref={bellRef} className="relative">
            <button
              onClick={handleBellClick}
              className="relative flex items-center justify-center w-9 h-9 rounded-xl transition-all"
              style={{ border: "1px solid var(--c-icon-btn-border)", color: "var(--c-icon-btn-color)" }}
              aria-label="通知"
            >
              🔔
              {notifications.length > 0 && (
                <span
                  className="absolute -top-1 -right-1 min-w-[16px] h-4 rounded-full text-[10px] font-bold text-white flex items-center justify-center px-1"
                  style={{ background: "rgba(239,68,68,0.92)" }}
                >
                  {notifications.length}
                </span>
              )}
            </button>

            {panelOpen && (
              <div
                className="absolute right-0 top-full mt-2 w-72 rounded-2xl p-3 space-y-2 z-50"
                style={{
                  background: "var(--c-panel-bg)",
                  backdropFilter: "blur(24px)",
                  border: "1px solid var(--c-panel-border)",
                  boxShadow: `0 12px 40px var(--c-panel-shadow)`,
                }}
              >
                <p
                  className="text-xs font-bold px-1"
                  style={{ color: "var(--c-panel-color)" }}
                >
                  通知 {notifications.length > 0 ? `(${notifications.length})` : ""}
                </p>
                {notifications.length === 0 ? (
                  <p
                    className="text-xs text-center py-3"
                    style={{ color: "var(--c-text-muted)" }}
                  >
                    通知はありません
                  </p>
                ) : (
                  notifications.map((n) => {
                    const c = NOTIFICATION_COLORS[n.type] ?? "rgba(148,163,184,";
                    return (
                      <Link
                        key={n.id}
                        href={`/projects/${n.projectId}`}
                        onClick={() => setPanelOpen(false)}
                        className="block rounded-xl p-2.5 text-xs font-medium transition-colors"
                        style={{
                          border: "1px solid var(--c-panel-item-border)",
                          color: "var(--c-panel-color)",
                        }}
                      >
                        <span
                          className="text-[10px] font-bold px-2 py-0.5 rounded-full mr-2"
                          style={{ background: `${c}0.15)`, color: `${c}0.95)` }}
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
                    ? "linear-gradient(135deg, rgba(251,146,60,0.18), rgba(249,115,22,0.18))"
                    : "var(--c-badge-bg)",
                  border: isAdmin ? "1px solid rgba(251,146,60,0.35)" : "1px solid var(--c-badge-border)",
                  color: isAdmin ? "rgba(251,146,60,0.95)" : "var(--c-badge-color)",
                }}
              >
                <span>{isAdmin ? "🔑" : "👤"}</span>
                <span className="hidden sm:inline">こんにちは、{user.display_name}さん</span>
              </div>
              <button
                onClick={logout}
                className="flex items-center justify-center w-9 h-9 rounded-xl text-xs font-bold transition-all"
                style={{ border: "1px solid var(--c-icon-btn-border)", color: "var(--c-icon-btn-color)" }}
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
              style={{ border: "1px solid var(--c-icon-btn-border)", color: "var(--c-nav-color)" }}
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
              background: "var(--c-nav-active-bg)",
              color: "var(--c-nav-active-color)",
              border: "1px solid var(--c-nav-active-border)",
            }
          : { color: "var(--c-nav-color)" }
      }
    >
      <span>{icon}</span>
      <span className="hidden sm:inline">{label}</span>
    </Link>
  );
}
