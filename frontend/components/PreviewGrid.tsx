"use client";

import Image from "next/image";
import type { EquipmentStatus } from "@/types";
import { getPhotoUrl } from "@/lib/api";

interface Props {
  equipment: EquipmentStatus[];
}

const EQUIPMENT_ICONS: Record<string, string> = {
  pos_register: "🖥️",
  cash_drawer: "💰",
  receipt_printer: "🖨️",
  router: "📡",
  lan_cabling: "🔌",
};

export default function PreviewGrid({ equipment }: Props) {
  return (
    <div className="space-y-5 stagger">
      {equipment.map((eq) => {
        const icon = EQUIPMENT_ICONS[eq.equipment_id] || "📦";
        return (
          <div key={eq.equipment_id}>
            {/* Equipment Header — Glass */}
            <div
              className="flex items-center gap-2.5 px-4 py-3 mb-3"
              style={{
                background: "rgba(99, 102, 241, 0.08)",
                backdropFilter: "blur(16px) saturate(180%)",
                WebkitBackdropFilter: "blur(16px) saturate(180%)",
                border: "1px solid rgba(255, 255, 255, 0.4)",
                borderRadius: "14px",
                boxShadow: "inset 0 1px 0 rgba(255,255,255,0.5), 0 2px 8px rgba(0,0,0,0.03)",
              }}
            >
              <div
                className="w-7 h-7 rounded-xl flex items-center justify-center text-sm"
                style={{
                  background: "rgba(99, 102, 241, 0.12)",
                  border: "1px solid rgba(255,255,255,0.4)",
                  boxShadow: "inset 0 1px 0 rgba(255,255,255,0.5)",
                }}
              >
                {icon}
              </div>
              <h3 className="text-sm font-bold text-gray-800">{eq.name}</h3>
            </div>

            {/* Photo Grid */}
            <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
              {eq.slots.map((slot) => (
                <div
                  key={`${eq.equipment_id}-${slot.slot_id}`}
                  className="card-liquid overflow-hidden"
                  style={{
                    background: slot.photo_filename
                      ? "rgba(16, 185, 129, 0.06)"
                      : "rgba(239, 68, 68, 0.04)",
                    backdropFilter: "blur(20px) saturate(180%)",
                    WebkitBackdropFilter: "blur(20px) saturate(180%)",
                    border: slot.photo_filename
                      ? "1.5px solid rgba(16, 185, 129, 0.3)"
                      : "1.5px solid rgba(239, 68, 68, 0.2)",
                    borderRadius: "16px",
                    boxShadow: slot.photo_filename
                      ? "inset 0 1px 0 rgba(255,255,255,0.5), 0 4px 12px rgba(16,185,129,0.08)"
                      : "inset 0 1px 0 rgba(255,255,255,0.4), 0 4px 12px rgba(0,0,0,0.03)",
                  }}
                >
                  {slot.photo_filename ? (
                    <div className="relative group">
                      <Image
                        src={getPhotoUrl(slot.photo_filename)}
                        alt={`${eq.name} - ${slot.label}`}
                        width={640}
                        height={360}
                        sizes="(max-width: 640px) 50vw, (max-width: 1024px) 33vw, 25vw"
                        className="w-full h-36 object-cover transition-transform duration-300 group-hover:scale-105"
                        style={{
                          transition: "transform 0.4s cubic-bezier(0.22, 1, 0.36, 1)",
                        }}
                      />
                      <div className="absolute top-2 right-2">
                        <div
                          className="w-5 h-5 rounded-lg flex items-center justify-center"
                          style={{
                            background: "linear-gradient(135deg, rgba(16,185,129,0.85), rgba(5,150,105,0.85))",
                            border: "1px solid rgba(255,255,255,0.4)",
                            boxShadow: "inset 0 1px 0 rgba(255,255,255,0.4), 0 2px 6px rgba(16,185,129,0.3)",
                          }}
                        >
                          <svg className="w-3 h-3 text-white" fill="currentColor" viewBox="0 0 20 20">
                            <path fillRule="evenodd" d="M16.707 5.293a1 1 0 010 1.414l-8 8a1 1 0 01-1.414 0l-4-4a1 1 0 011.414-1.414L8 12.586l7.293-7.293a1 1 0 011.414 0z" clipRule="evenodd" />
                          </svg>
                        </div>
                      </div>
                    </div>
                  ) : (
                    <div
                      className="w-full h-36 flex flex-col items-center justify-center"
                      style={{
                        background: "rgba(239, 68, 68, 0.04)",
                      }}
                    >
                      <svg className="w-8 h-8 text-red-300/50 mb-1" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-2.5L13.732 4.5c-.77-.833-2.694-.833-3.464 0L3.34 16.5c-.77.833.192 2.5 1.732 2.5z" />
                      </svg>
                      <span className="text-xs text-red-300/70 font-medium">未撮影</span>
                    </div>
                  )}
                  <div
                    className="px-3 py-2 text-center"
                    style={{
                      borderTop: "1px solid rgba(255,255,255,0.25)",
                      background: "rgba(255,255,255,0.08)",
                    }}
                  >
                    <span className="text-xs font-bold text-gray-600/80">{slot.label}</span>
                  </div>
                </div>
              ))}
            </div>
          </div>
        );
      })}
    </div>
  );
}
