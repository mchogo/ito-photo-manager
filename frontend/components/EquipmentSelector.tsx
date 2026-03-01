"use client";

import type { EquipmentDef } from "@/types";

interface Props {
  equipment: EquipmentDef[];
  selected: Set<string>;
  onToggle: (equipmentId: string) => void;
}

const EQUIPMENT_ICONS: Record<string, string> = {
  pos_register: "🖥️",
  cash_drawer: "💰",
  receipt_printer: "🖨️",
  router: "📡",
  lan_cabling: "🔌",
};

export default function EquipmentSelector({ equipment, selected, onToggle }: Props) {
  return (
    <div className="space-y-3 stagger">
      {equipment.map((eq) => {
        const isSelected = selected.has(eq.equipment_id);
        const icon = EQUIPMENT_ICONS[eq.equipment_id] || "📦";
        return (
          <label
            key={eq.equipment_id}
            className={`card-liquid flex items-center gap-4 p-4 rounded-2xl cursor-pointer ${
              isSelected ? "liquid-glass-deep" : ""
            }`}
            style={{
              background: isSelected
                ? "rgba(99, 102, 241, 0.1)"
                : "rgba(255, 255, 255, 0.15)",
              backdropFilter: "blur(20px) saturate(180%)",
              WebkitBackdropFilter: "blur(20px) saturate(180%)",
              border: isSelected
                ? "1.5px solid rgba(99, 102, 241, 0.35)"
                : "1px solid rgba(255, 255, 255, 0.35)",
              boxShadow: isSelected
                ? "inset 0 1px 0 rgba(255,255,255,0.5), 0 4px 20px rgba(99,102,241,0.12)"
                : "inset 0 1px 0 rgba(255,255,255,0.4), 0 2px 8px rgba(0,0,0,0.03)",
              borderRadius: "18px",
            }}
          >
            {/* Icon */}
            <div
              className="w-12 h-12 rounded-2xl flex items-center justify-center text-2xl shrink-0"
              style={{
                background: isSelected
                  ? "rgba(99, 102, 241, 0.15)"
                  : "rgba(255, 255, 255, 0.25)",
                border: "1px solid rgba(255,255,255,0.4)",
                boxShadow: "inset 0 1px 0 rgba(255,255,255,0.5)",
                transition: "transform 0.3s cubic-bezier(0.34, 1.56, 0.64, 1)",
                transform: isSelected ? "scale(1.08)" : "scale(1)",
              }}
            >
              {icon}
            </div>

            {/* Text */}
            <div className="flex-1 min-w-0">
              <div className="font-bold text-[15px] text-gray-800">{eq.name}</div>
              <div className="text-xs text-gray-500/70 mt-0.5 font-medium">
                {eq.photo_slots.map((s) => s.label).join(" / ")}
              </div>
            </div>

            {/* Badge + Check */}
            <div className="flex items-center gap-2.5 shrink-0">
              <span
                className="text-xs font-bold px-2.5 py-1 rounded-full"
                style={{
                  background: isSelected
                    ? "rgba(99,102,241,0.2)"
                    : "rgba(0,0,0,0.05)",
                  color: isSelected ? "#4f46e5" : "#64748b",
                }}
              >
                {eq.photo_slots.length}枚
              </span>
              <div
                className="w-6 h-6 rounded-xl flex items-center justify-center"
                style={{
                  background: isSelected
                    ? "linear-gradient(135deg, rgba(99,102,241,0.85), rgba(139,92,246,0.85))"
                    : "rgba(255,255,255,0.3)",
                  border: isSelected
                    ? "1px solid rgba(255,255,255,0.4)"
                    : "1.5px solid rgba(0,0,0,0.12)",
                  boxShadow: isSelected
                    ? "inset 0 1px 0 rgba(255,255,255,0.4), 0 2px 6px rgba(99,102,241,0.25)"
                    : "inset 0 1px 0 rgba(255,255,255,0.3)",
                  transition: "all 0.3s cubic-bezier(0.34, 1.56, 0.64, 1)",
                  transform: isSelected ? "scale(1.1)" : "scale(1)",
                }}
              >
                {isSelected && (
                  <svg className="w-3.5 h-3.5 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={3}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
                  </svg>
                )}
              </div>
            </div>

            <input
              type="checkbox"
              checked={isSelected}
              onChange={() => onToggle(eq.equipment_id)}
              className="sr-only"
            />
          </label>
        );
      })}
    </div>
  );
}
