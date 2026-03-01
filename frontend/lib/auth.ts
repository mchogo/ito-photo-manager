"use client";

import type { AuthUser } from "@/types";

const TOKEN_KEY = "pm_authToken";

export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string): void {
  localStorage.setItem(TOKEN_KEY, token);
}

export function clearToken(): void {
  localStorage.removeItem(TOKEN_KEY);
}

/** JWT payload をデコードしてキャッシュされたユーザー情報を返す（署名検証なし）*/
export function getStoredUser(): AuthUser | null {
  const token = getToken();
  if (!token) return null;
  try {
    const parts = token.split(".");
    if (parts.length !== 3) return null;
    const payload = JSON.parse(atob(parts[1].replace(/-/g, "+").replace(/_/g, "/")));
    if (!payload.sub || !payload.role || !payload.display_name) return null;
    return {
      user_id: payload.sub,
      username: payload.display_name, // display_name をフォールバックとして使用
      display_name: payload.display_name,
      role: payload.role,
    };
  } catch {
    return null;
  }
}
