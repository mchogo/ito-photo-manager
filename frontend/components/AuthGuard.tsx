"use client";

import { useEffect } from "react";
import { usePathname, useRouter } from "next/navigation";
import { getToken } from "@/lib/auth";

/** 未ログイン時は /login にリダイレクトするガード（/login 自体は除外） */
export default function AuthGuard({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();

  useEffect(() => {
    if (pathname === "/login") return;
    if (!getToken()) {
      router.replace("/login");
    }
  }, [pathname, router]);

  // /login ページは常に表示
  if (pathname === "/login") return <>{children}</>;

  // トークンなし = ガード中（リダイレクト前の一瞬）は何も表示しない
  if (!getToken()) return null;

  return <>{children}</>;
}
