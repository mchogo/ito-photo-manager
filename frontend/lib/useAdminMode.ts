"use client";

import { useAuth } from "./useAuth";

/**
 * Phase 4 後方互換シム: JWT role を読んで管理者かどうかを返す。
 * toggle は no-op（JWT ベースなのでトグルは廃止）。
 * 既存の ShootTab・DocsTab・useNotifications などは変更不要。
 */
export function useAdminMode(): [boolean, () => void] {
  const { isAdmin } = useAuth();
  return [isAdmin, () => {}];
}
