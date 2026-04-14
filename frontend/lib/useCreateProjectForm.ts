"use client";

import { useCallback, useMemo, useState } from "react";
import { createProject } from "@/lib/api";
import type { EquipmentDef } from "@/types";

const LS_KEY_SITE_ID = "pm_siteId";
const LS_KEY_WORKER = "pm_workerName";
const LS_KEY_WORK_DATE = "pm_workDate";

function getStoredValue(key: string): string | null {
  if (typeof window === "undefined") return null;
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function setStoredValue(key: string, value: string): void {
  if (typeof window === "undefined") return;
  try {
    localStorage.setItem(key, value);
  } catch {
    // no-op
  }
}

type UseCreateProjectFormParams = {
  equipment: EquipmentDef[];
  onCreated: (projectId: string) => void;
  onError: (message: string | null) => void;
};

export function useCreateProjectForm({ equipment, onCreated, onError }: UseCreateProjectFormParams) {
  const [submitting, setSubmitting] = useState(false);
  const [siteId, setSiteId] = useState(() => getStoredValue(LS_KEY_SITE_ID) ?? "");
  const [workDate, setWorkDate] = useState(
    () => getStoredValue(LS_KEY_WORK_DATE) || new Date().toISOString().slice(0, 10),
  );
  const [workerName, setWorkerName] = useState(() => getStoredValue(LS_KEY_WORKER) ?? "");
  const [selectedEquipment, setSelectedEquipment] = useState<Set<string>>(new Set());

  const handleSiteIdChange = useCallback((value: string) => {
    setSiteId(value);
    setStoredValue(LS_KEY_SITE_ID, value);
  }, []);

  const handleWorkerNameChange = useCallback((value: string) => {
    setWorkerName(value);
    setStoredValue(LS_KEY_WORKER, value);
  }, []);

  const handleWorkDateChange = useCallback((value: string) => {
    setWorkDate(value);
    setStoredValue(LS_KEY_WORK_DATE, value);
  }, []);

  const handleToggle = useCallback((id: string) => {
    setSelectedEquipment((prev) => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  }, []);

  const canSubmit = Boolean(
    siteId.trim() && workerName.trim() && workDate && selectedEquipment.size > 0 && !submitting,
  );

  const totalPhotos = useMemo(
    () =>
      equipment
        .filter((eq) => selectedEquipment.has(eq.equipment_id))
        .reduce((sum, eq) => sum + eq.photo_slots.length, 0),
    [equipment, selectedEquipment],
  );

  const handleSubmit = useCallback(async () => {
    if (!canSubmit) return;
    setSubmitting(true);
    onError(null);
    try {
      const project = await createProject({
        site_id: siteId.trim(),
        work_date: workDate,
        worker_name: workerName.trim(),
        equipment_ids: Array.from(selectedEquipment),
      });
      onCreated(project.project_id);
    } catch (e: unknown) {
      onError(e instanceof Error ? e.message : "案件の作成に失敗しました");
      setSubmitting(false);
    }
  }, [canSubmit, onCreated, onError, selectedEquipment, siteId, workDate, workerName]);

  return {
    submitting,
    siteId,
    workDate,
    workerName,
    selectedEquipment,
    canSubmit,
    totalPhotos,
    handleSiteIdChange,
    handleWorkerNameChange,
    handleWorkDateChange,
    handleToggle,
    handleSubmit,
  };
}
