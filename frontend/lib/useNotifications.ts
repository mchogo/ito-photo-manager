"use client";

import { useCallback, useEffect, useState } from "react";
import { listProjects } from "@/lib/api";
import type { Project } from "@/types";
import { getStoredUser } from "./auth";

export interface AppNotification {
  id: string;
  type: "入店リマインド" | "図書催促" | "再撮影指示";
  projectId: string;
  projectName: string;
  message: string;
}

function computeNotifications(projects: Project[]): AppNotification[] {
  const now = Date.now();
  const result: AppNotification[] = [];

  for (const p of projects) {
    const name = p.project_name || p.site_id;

    // 1. 入店リマインド: work_start_time まで30分以内 && arrival_time が null
    if (p.work_start_time && !p.arrival_time) {
      const startMs = new Date(p.work_start_time).getTime();
      const diff = startMs - now;
      if (diff >= 0 && diff <= 30 * 60 * 1000) {
        result.push({
          id: `remind-${p.project_id}`,
          type: "入店リマインド",
          projectId: p.project_id,
          projectName: name,
          message: `${name} — 入店予定まであと${Math.ceil(diff / 60000)}分`,
        });
      }
    }

    // 2. 図書催促: checkout_time あり && 完成図書なし && 2時間以上経過
    if (p.checkout_time) {
      const elapsed = now - new Date(p.checkout_time).getTime();
      const hasKansho = (p.documents ?? []).some(
        (d) => d.document_type === "完成図書_調査" || d.document_type === "完成図書_設置",
      );
      if (!hasKansho && elapsed > 2 * 60 * 60 * 1000) {
        result.push({
          id: `tosho-${p.project_id}`,
          type: "図書催促",
          projectId: p.project_id,
          projectName: name,
          message: `${name} — 完成図書が未提出です`,
        });
      }
    }

    // 3. 再撮影指示: 任意スロットに retake_instruction あり
    const hasRetake = p.equipment.some((eq) =>
      eq.slots.some((sl) => sl.retake_instruction !== null),
    );
    if (hasRetake) {
      result.push({
        id: `retake-${p.project_id}`,
        type: "再撮影指示",
        projectId: p.project_id,
        projectName: name,
        message: `${name} — 再撮影指示があります`,
      });
    }
  }

  return result;
}

/** 通知を計算するフック
 * - pm_workerName が設定されていれば自分の案件のみ
 * - 管理者モード (pm_adminMode=true) 時は全案件
 */
export function useNotifications() {
  const [notifications, setNotifications] = useState<AppNotification[]>([]);

  const refresh = useCallback(async () => {
    try {
      const user = getStoredUser();
      // 未ログイン時はスキップ（認証必須エンドポイントへの不要なリクエストを防ぐ）
      if (!user) return;
      const isAdmin = user.role === "admin";
      const workerName = user.display_name ?? localStorage.getItem("pm_workerName") ?? undefined;
      const filters = isAdmin || !workerName ? {} : { worker_name: workerName };
      const projects = await listProjects(filters);
      setNotifications(computeNotifications(projects));
    } catch {
      // 通知は非クリティカルなので silent fail
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  return { notifications, refresh };
}
