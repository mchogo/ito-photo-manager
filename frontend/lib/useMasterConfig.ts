"use client";

import { useCallback, useEffect, useState } from "react";
import { getMasterConfig, updateDocumentTypes, updateStatuses } from "@/lib/api";
import type { MasterConfig, MasterConfigDocType, MasterConfigStatus } from "@/types";
import {
  COLOR_PALETTE,
  DOCUMENT_CATEGORIES,
  PROJECT_STATUSES,
  STATUS_COLORS,
} from "@/types";

/** フォールバック設定（APIが未ロードの場合に使用） */
const FALLBACK_CONFIG: MasterConfig = {
  statuses: PROJECT_STATUSES.map((value) => ({
    value,
    color: Object.entries(STATUS_COLORS).find(([k]) => k === value)?.[1]?.split(" ")[0]?.replace("bg-", "").replace("-100", "") ?? "gray",
  })),
  document_types: [
    ...DOCUMENT_CATEGORIES.管理共有.map((v) => ({ value: v, category: "管理共有" as const })),
    ...DOCUMENT_CATEGORIES.現地調査.map((v) => ({ value: v, category: "現地調査" as const })),
    ...DOCUMENT_CATEGORIES.設置.map((v) => ({ value: v, category: "設置" as const })),
  ],
};

export function useMasterConfig() {
  const [config, setConfig] = useState<MasterConfig>(FALLBACK_CONFIG);

  const reload = useCallback(async () => {
    try {
      const data = await getMasterConfig();
      setConfig(data);
    } catch {
      // 未ログイン・通信エラー時はフォールバックを保持
    }
  }, []);

  useEffect(() => {
    const timer = setTimeout(() => {
      void reload();
    }, 0);
    return () => clearTimeout(timer);
  }, [reload]);

  /** ステータスのバッジクラスを返す（未知の場合は gray） */
  const colorOf = useCallback(
    (status: string): string => {
      const found = config.statuses.find((s) => s.value === status);
      return COLOR_PALETTE[found?.color ?? "gray"] ?? COLOR_PALETTE.gray;
    },
    [config.statuses],
  );

  /** カテゴリごとに書類種別を返す */
  const docTypesByCategory = useCallback(
    (category: "管理共有" | "現地調査" | "設置"): string[] =>
      config.document_types.filter((d) => d.category === category).map((d) => d.value),
    [config.document_types],
  );

  const saveStatuses = useCallback(
    async (statuses: MasterConfigStatus[]) => {
      const updated = await updateStatuses(statuses);
      setConfig(updated);
    },
    [],
  );

  const saveDocumentTypes = useCallback(
    async (docTypes: MasterConfigDocType[]) => {
      const updated = await updateDocumentTypes(docTypes);
      setConfig(updated);
    },
    [],
  );

  return { config, colorOf, docTypesByCategory, saveStatuses, saveDocumentTypes, reload };
}
