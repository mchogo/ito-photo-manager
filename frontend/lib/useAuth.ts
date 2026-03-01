"use client";

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import type { AuthUser } from "@/types";
import { clearToken, getStoredUser } from "./auth";
import { login as apiLogin } from "./api";

interface AuthState {
  user: AuthUser | null;
  isLoading: boolean;
}

export function useAuth() {
  const [state, setState] = useState<AuthState>({ user: null, isLoading: true });
  const router = useRouter();

  useEffect(() => {
    // JWT payload から即座にユーザー情報を取得（API コール不要）
    const user = getStoredUser();
    setState({ user, isLoading: false });
  }, []);

  const login = useCallback(async (username: string, password: string) => {
    await apiLogin(username, password);
    const user = getStoredUser();
    setState({ user, isLoading: false });
  }, []);

  const logout = useCallback(() => {
    clearToken();
    setState({ user: null, isLoading: false });
    router.push("/login");
  }, [router]);

  return {
    user: state.user,
    isAdmin: state.user?.role === "admin",
    isLoading: state.isLoading,
    login,
    logout,
  };
}

/** 未認証ならば /login へリダイレクトするフック */
export function useRequireAuth() {
  const auth = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (!auth.isLoading && !auth.user) {
      router.push("/login");
    }
  }, [auth.isLoading, auth.user, router]);

  return auth;
}
