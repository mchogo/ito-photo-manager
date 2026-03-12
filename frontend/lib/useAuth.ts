"use client";

import { useAuthContext } from "./AuthContext";

export function useAuth() {
  return useAuthContext();
}

export function useRequireAuth() {
  return useAuthContext();
}
