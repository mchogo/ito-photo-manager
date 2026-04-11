from __future__ import annotations

import os
from datetime import datetime
from typing import Any

import requests
import streamlit as st


def _default_api_base() -> str:
    secret_val = st.secrets.get("API_BASE_URL", "")
    env_val = os.getenv("API_BASE_URL", "")
    base = secret_val or env_val or "http://localhost:8000"
    return str(base).rstrip("/")


def _init_session() -> None:
    if "api_base" not in st.session_state:
        st.session_state.api_base = _default_api_base()
    if "token" not in st.session_state:
        st.session_state.token = ""
    if "me" not in st.session_state:
        st.session_state.me = None


def _headers() -> dict[str, str]:
    token = st.session_state.get("token", "")
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}"}


def _api_request(method: str, path: str, **kwargs: Any) -> requests.Response:
    base = st.session_state.api_base.rstrip("/")
    url = f"{base}{path}"
    headers = kwargs.pop("headers", {})
    merged_headers = {**_headers(), **headers}
    timeout = kwargs.pop("timeout", 20)
    return requests.request(method, url, headers=merged_headers, timeout=timeout, **kwargs)


def _login(username: str, password: str) -> tuple[bool, str]:
    try:
        res = _api_request(
            "POST",
            "/api/auth/login",
            json={"username": username, "password": password},
            headers={"Content-Type": "application/json"},
        )
    except requests.RequestException as e:
        return False, f"接続に失敗しました: {e}"

    if res.status_code != 200:
        try:
            detail = res.json().get("detail")
            if isinstance(detail, dict) and detail.get("message"):
                return False, str(detail["message"])
        except Exception:
            pass
        return False, f"ログイン失敗 ({res.status_code})"

    data = res.json()
    st.session_state.token = data.get("access_token", "")
    return True, ""


def _fetch_me() -> tuple[bool, str]:
    try:
        res = _api_request("GET", "/api/auth/me")
    except requests.RequestException as e:
        return False, f"接続に失敗しました: {e}"

    if res.status_code != 200:
        return False, f"ユーザー情報取得失敗 ({res.status_code})"

    st.session_state.me = res.json()
    return True, ""


def _list_projects(
    status: str = "",
    worker_name: str = "",
    scheduled_date: str = "",
) -> tuple[list[dict[str, Any]], str]:
    params: dict[str, str] = {}
    if status:
        params["status"] = status
    if worker_name:
        params["worker_name"] = worker_name
    if scheduled_date:
        params["scheduled_date"] = scheduled_date

    try:
        res = _api_request("GET", "/api/projects", params=params)
    except requests.RequestException as e:
        return [], f"接続に失敗しました: {e}"

    if res.status_code != 200:
        return [], f"案件一覧取得失敗 ({res.status_code})"
    return res.json(), ""


def _load_site_master() -> tuple[dict[str, Any], str]:
    try:
        res = _api_request("GET", "/api/reference/site-master")
    except requests.RequestException as e:
        return {}, f"接続に失敗しました: {e}"
    if res.status_code != 200:
        return {}, f"拠点マスタ取得失敗 ({res.status_code})"
    return res.json(), ""


def _load_request_template() -> tuple[dict[str, Any], str]:
    try:
        res = _api_request("GET", "/api/reference/request-sheet-template")
    except requests.RequestException as e:
        return {}, f"接続に失敗しました: {e}"
    if res.status_code != 200:
        return {}, f"依頼シートテンプレ取得失敗 ({res.status_code})"
    return res.json(), ""


def _format_iso_date(iso_str: str | None) -> str:
    if not iso_str:
        return ""
    try:
        return datetime.fromisoformat(iso_str.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return iso_str


def render_sidebar() -> None:
    with st.sidebar:
        st.header("接続設定")
        api_base = st.text_input("API Base URL", value=st.session_state.api_base)
        st.session_state.api_base = api_base.rstrip("/")
        st.caption("例: https://your-render-service.onrender.com")

        if st.session_state.token:
            if st.button("ログアウト", use_container_width=True):
                st.session_state.token = ""
                st.session_state.me = None
                st.rerun()


def render_login() -> None:
    st.subheader("ログイン")
    with st.form("login_form", clear_on_submit=False):
        username = st.text_input("ユーザー名")
        password = st.text_input("パスワード", type="password")
        submitted = st.form_submit_button("ログイン")
        if submitted:
            ok, msg = _login(username.strip(), password)
            if not ok:
                st.error(msg)
                return
            ok, msg = _fetch_me()
            if not ok:
                st.error(msg)
                return
            st.success("ログインしました")
            st.rerun()


def render_projects_tab() -> None:
    st.markdown("### 案件一覧")
    col1, col2, col3 = st.columns([2, 2, 2])
    with col1:
        status = st.text_input("ステータス", value="")
    with col2:
        worker_name = st.text_input("作業員名", value="")
    with col3:
        scheduled_date = st.text_input("予定日 (YYYY-MM-DD)", value="")

    if st.button("案件を取得", use_container_width=True):
        projects, err = _list_projects(status, worker_name, scheduled_date)
        if err:
            st.error(err)
            return
        rows: list[dict[str, Any]] = []
        for p in projects:
            rows.append(
                {
                    "project_id": p.get("project_id"),
                    "site_id": p.get("site_id"),
                    "project_name": p.get("project_name"),
                    "worker_name": p.get("worker_name"),
                    "status": p.get("status"),
                    "scheduled_date": p.get("scheduled_date"),
                    "work_start_time": _format_iso_date(p.get("work_start_time")),
                    "arrival_time": _format_iso_date(p.get("arrival_time")),
                    "checkout_time": _format_iso_date(p.get("checkout_time")),
                }
            )
        st.write(f"{len(rows)} 件")
        st.dataframe(rows, use_container_width=True, hide_index=True)


def render_site_master_tab() -> None:
    st.markdown("### 拠点マスタ")
    if st.button("拠点マスタを取得", use_container_width=True):
        data, err = _load_site_master()
        if err:
            st.error(err)
            return
        records = data.get("records", [])
        st.caption(f"件数: {data.get('record_count', len(records))}")
        search = st.text_input("営業所名で検索", value="")
        if search:
            records = [r for r in records if search in str(r.get("営業所", ""))]
        st.dataframe(records, use_container_width=True, hide_index=True)


def render_request_template_tab() -> None:
    st.markdown("### 依頼シートテンプレ")
    if st.button("依頼シートテンプレを取得", use_container_width=True):
        data, err = _load_request_template()
        if err:
            st.error(err)
            return
        template = data.get("template", {})
        st.write(f"タイトル: {template.get('title', '')}")
        for section in template.get("sections", []):
            with st.expander(f"{section.get('name', '')} ({section.get('id', '')})", expanded=False):
                for note in section.get("notes", []):
                    st.markdown(f"- {note}")
                for note in section.get("usage_notes", []):
                    st.markdown(f"- {note}")
                fields = section.get("fields", [])
                if fields:
                    st.table(fields)


def main() -> None:
    st.set_page_config(page_title="ito-photo-manager client", layout="wide")
    _init_session()
    render_sidebar()

    st.title("ito-photo-manager Streamlit Client")
    st.caption("Streamlit から Render/FastAPI バックエンドに接続して利用します。")

    if not st.session_state.token:
        render_login()
        return

    if st.session_state.me is None:
        ok, msg = _fetch_me()
        if not ok:
            st.error(msg)
            return

    me = st.session_state.me or {}
    st.success(f"ログイン中: {me.get('display_name', '')} ({me.get('role', '')})")

    tab1, tab2, tab3 = st.tabs(["案件一覧", "拠点マスタ", "依頼シートテンプレ"])
    with tab1:
        render_projects_tab()
    with tab2:
        render_site_master_tab()
    with tab3:
        render_request_template_tab()


if __name__ == "__main__":
    main()

