# streamlit_claim_compare_app_v3.py
# -*- coding: utf-8 -*-
# -------------------------------------------------------------
# 스트림릿 앱 v3: 더 강한 예외 처리 + 진단 모드
# - 앱 전체 try/except로 감싸 전역 오류도 화면에 표시
# - 업로드 파일 바이트 길이/헤더 진단
# - XLSX만 지원(비-XLSX는 친절한 안내)
# -------------------------------------------------------------

import io
import re
import sys
from typing import Dict, List, Optional

import pandas as pd
import streamlit as st

def app():
    st.set_page_config(page_title="EDI 청구통계 월별 비교 (v3)", layout="wide")
    diag = st.sidebar.checkbox("🔧 진단 모드(세부 로그 표시)", value=True)

    def dprint(msg: str):
        if diag:
            st.sidebar.write(msg)

    with st.expander("📌 사용 안내 (요약)", expanded=True):
        st.markdown(
            """
            - 파일명 규칙: **의사별_8월.xlsx**, **청구_8월.xlsx**(또는 청구별)  
            - 비교 방식: 업로드된 월 중 **최신=당월**, 직전=전달 인식  
            - 합산: `본인부담상한초과 + 청구액 + 지원금 + 장애인의료비 + 보훈청구액 + 보훈감면액 + 100/100미만보훈청구`  
            - 표시: 증가 `▼`, 감소 `▲`, 동일 `—`
            """
        )

    st.subheader("📤 XLSX 업로드(.xlsx 전용)")
    uploaded_files = st.file_uploader(
        "여러 개 파일을 동시에 올릴 수 있습니다.",
        type=["xlsx"],
        accept_multiple_files=True,
    )

    # 더미 데이터
    if st.button("🎯 더미데이터 시연"):
        _mk_dummy()
        st.success("더미데이터 적재 완료. 아래에서 비교 실행.")

    # 유틸/상수
    MONTH_RE = re.compile(r"(?:^|[^0-9])(\d{1,2})\s*월", re.I)
    SUM_COLS = ["본인부담상한초과","청구액","지원금","장애인의료비","보훈청구액","보훈감면액","100/100미만보훈청구"]
    RENAME = {
        "보험유형": ["보험유형","보험 구분","보험구분","보험-구분","보험_구분","보험종류"],
        "입원/외래": ["입원/외래","입/외","입원외래","입원_외래","입원 · 외래","입원-외래"],
        "청구차수": ["청구차수","청구 차수","청구_차수","청구-차수"],
    }

    def parse_mm(name:str)->Optional[int]:
        m = MONTH_RE.search(name or "")
        if not m: return None
        try:
            mm = int(m.group(1)); 
            return mm if 1<=mm<=12 else None
        except: 
            return None

    def detect_kind(name:str)->Optional[str]:
        nm = (name or "").lower()
        if "의사별" in name: return "doctor"
        if ("청구별" in name) or ("청구" in name) or ("claim" in nm): return "claim"
        return None

    def normalize(df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df.columns = [str(c).strip() for c in df.columns]
        # 동의어 매핑
        newcols = {}
        for target, aliases in RENAME.items():
            for al in aliases:
                if al in df.columns and target not in df.columns:
                    newcols[al] = target
                    break
        if newcols:
            df = df.rename(columns=newcols)
            dprint(f"[정규화] {newcols}")
        return df

    def to_num(s: pd.Series)->pd.Series:
        return pd.to_numeric(s.astype(str).str.replace(",", "", regex=False), errors="coerce").fillna(0)

    def prepare(df: pd.DataFrame)->pd.DataFrame:
        df = normalize(df)
        for c in SUM_COLS:
            if c not in df.columns:
                df[c] = 0
        for c in SUM_COLS:
            df[c] = to_num(df[c])
        df["__합산청구액__"] = df[SUM_COLS].sum(axis=1)
        return df

    def gsum(df: pd.DataFrame, by: str)->pd.DataFrame:
        if by not in df.columns:
            df = df.copy()
            df[by] = "미지정"
            st.warning(f"'{by}' 컬럼이 없어 '미지정'으로 집계합니다.")
        g = df.groupby(by, dropna=False)["__합산청구액__"].sum().reset_index()
        return g.rename(columns={by:"구분","__합산청구액__":"청구액"})

    def compare(prev: pd.DataFrame, curr: pd.DataFrame)->pd.DataFrame:
        merged = pd.merge(prev, curr, on="구분", how="outer", suffixes=("_전달","_당월")).fillna(0)
        merged["증감"] = merged["청구액_당월"] - merged["청구액_전달"]
        def mark(x):
            if x>0: return f"▼{int(abs(x)):,}"
            if x<0: return f"▲{int(abs(x)):,}"
            return "—"
        merged["증감(기호)"] = merged["증감"].apply(mark)
        return merged[["구분","청구액_전달","청구액_당월","증감(기호)","증감"]].sort_values("구분").reset_index(drop=True)

    def read_uploaded(upl)->pd.DataFrame:
        # 진단: 파일 이름/크기/헤더
        fname = getattr(upl, "name", "(unknown)")
        raw = upl.read()
        dprint(f"[업로드] {fname} bytes={len(raw)}")
        if len(raw) < 4:
            raise ValueError(f"{fname}: 파일 크기가 비정상적으로 작습니다.")
        head = raw[:4]
        # XLSX(zip) 시그니처: 50 4B 03 04 (PK..)
        if head[:2] != b"PK":
            raise ValueError(f"{fname}: XLSX 형식이 아닙니다(확장자는 .xlsx여도 실제는 xls/csv일 수 있음). 엑셀에서 '다른 이름으로 저장' → .xlsx로 변환 후 업로드하세요.")
        bio = io.BytesIO(raw)
        return pd.read_excel(bio, sheet_name=0, dtype=str, engine="openpyxl")

    # 버킷
    buckets: Dict[str, Dict[int, List[pd.DataFrame]]] = {"doctor": {}, "claim": {}}

    # 더미 합류
    if "dummy" in st.session_state:
        for kind in ("doctor","claim"):
            for m, dfs in st.session_state["dummy"][kind].items():
                buckets.setdefault(kind, {}).setdefault(m, []).extend(dfs)
        dprint("더미데이터 합류")

    # 업로드 적재
    if uploaded_files:
        for upl in uploaded_files:
            try:
                name = upl.name
                kind = detect_kind(name)
                mm = parse_mm(name)
                if not kind or not mm:
                    st.warning(f"무시됨: `{name}` (종류/월 인식 실패)")
                    dprint(f"무시: {name} kind={kind} mm={mm}")
                    continue
                df = read_uploaded(upl)
                df = prepare(df)
                buckets.setdefault(kind, {}).setdefault(mm, []).append(df)
                st.success(f"인식됨: `{name}` → {kind}/{mm}월, rows={len(df)}")
            except Exception as e:
                st.exception(e)

    # 화면
    doc_months = sorted(buckets["doctor"].keys())
    claim_months = sorted(buckets["claim"].keys())

    c1, c2 = st.columns(2)
    with c1:
        st.markdown("### 🩺 의사별")
        if not doc_months:
            st.info("의사별 파일이 없습니다.")
        else:
            curr = max(doc_months)
            prevs = [m for m in doc_months if m<curr]
            prev = max(prevs) if prevs else None
            st.caption(f"자동 인식 → 당월: **{curr}월**, 전달: **{prev or '없음'}**")
            if st.button("의사별 비교 실행"):
                try:
                    p = _cat(buckets["doctor"].get(prev, []))
                    c = _cat(buckets["doctor"].get(curr, []))
                    if p.empty or c.empty:
                        st.error("의사별 비교에 필요한 월 데이터가 부족합니다.")
                    else:
                        out = compare(gsum(p, "청구차수"), gsum(c, "청구차수"))
                        _show_df(out, "의사별(청구차수)")
                        st.session_state["out_doc"] = out
                        st.session_state["out_doc_months"] = (prev, curr)
                except Exception as e:
                    st.exception(e)

    with c2:
        st.markdown("### 📊 청구 기준")
        if not claim_months:
            st.info("청구/청구별 파일이 없습니다.")
        else:
            curr = max(claim_months)
            prevs = [m for m in claim_months if m<curr]
            prev = max(prevs) if prevs else None
            st.caption(f"자동 인식 → 당월: **{curr}월**, 전달: **{prev or '없음'}**")

            cc1, cc2 = st.columns(2)
            with cc1:
                if st.button("보험유형 기준 비교 실행"):
                    try:
                        p = _cat(buckets["claim"].get(prev, []))
                        c = _cat(buckets["claim"].get(curr, []))
                        if p.empty or c.empty:
                            st.error("비교에 필요한 월 데이터가 부족합니다.")
                        else:
                            out = compare(gsum(p, "보험유형"), gsum(c, "보험유형"))
                            _show_df(out, "보험유형")
                            st.session_state["out_ins"] = out
                            st.session_state["out_ins_months"] = (prev, curr)
                    except Exception as e:
                        st.exception(e)
            with cc2:
                if st.button("입원·외래 기준 비교 실행"):
                    try:
                        p = _cat(buckets["claim"].get(prev, []))
                        c = _cat(buckets["claim"].get(curr, []))
                        if p.empty or c.empty:
                            st.error("비교에 필요한 월 데이터가 부족합니다.")
                        else:
                            out = compare(gsum(p, "입원/외래"), gsum(c, "입원/외래"))
                            _show_df(out, "입원·외래")
                            st.session_state["out_io"] = out
                            st.session_state["out_io_months"] = (prev, curr)
                    except Exception as e:
                        st.exception(e)

    st.markdown("---")
    st.subheader("📥 엑셀로 내보내기")
    try:
        _download()
    except Exception as e:
        st.exception(e)

    if diag:
        st.sidebar.markdown("---")
        st.sidebar.write(f"Python: {sys.version.split()[0]}")
        st.sidebar.write(f"pandas: {pd.__version__}")
        try:
            import openpyxl
            st.sidebar.write(f"openpyxl: {openpyxl.__version__}")
        except Exception as e:
            st.sidebar.write(f"openpyxl import 실패: {e}")

def _cat(dfs: List[pd.DataFrame])->pd.DataFrame:
    import pandas as pd
    if not dfs: 
        return pd.DataFrame()
    return pd.concat(dfs, ignore_index=True)

def _show_df(df: pd.DataFrame, title: str):
    import streamlit as st
    st.markdown(f"#### 결과표 — {title}")
    st.dataframe(
        df.style.format({
            "청구액_전달":"{:,.0f}",
            "청구액_당월":"{:,.0f}",
            "증감":"{:,.0f}",
        }),
        use_container_width=True,
    )

def _download():
    import io, pandas as pd, streamlit as st
    if not (("out_doc" in st.session_state) or ("out_ins" in st.session_state) or ("out_io" in st.session_state)):
        st.info("먼저 비교 실행 버튼을 눌러 결과를 생성하세요.")
        return
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        if "out_doc" in st.session_state:
            p,c = st.session_state.get("out_doc_months",(None,None))
            st.session_state["out_doc"].to_excel(xw, sheet_name=f"의사별({p}→{c})", index=False)
        if "out_ins" in st.session_state:
            p,c = st.session_state.get("out_ins_months",(None,None))
            st.session_state["out_ins"].to_excel(xw, sheet_name=f"보험유형({p}→{c})", index=False)
        if "out_io" in st.session_state:
            p,c = st.session_state.get("out_io_months",(None,None))
            st.session_state["out_io"].to_excel(xw, sheet_name=f"입원외래({p}→{c})", index=False)
    buf.seek(0)
    st.download_button(
        "⬇️ 비교 결과 엑셀 다운로드",
        data=buf,
        file_name="청구통계_월별비교_결과.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

def _mk_dummy():
    import pandas as pd, streamlit as st
    df_doc_8 = pd.DataFrame({
        "청구차수": ["1차","2차","3차"],
        "본인부담상한초과":[0,0,0],
        "청구액":[1000000,800000,500000],
        "지원금":[0,0,0],
        "장애인의료비":[0,0,0],
        "보훈청구액":[0,0,0],
        "보훈감면액":[0,0,0],
        "100/100미만보훈청구":[0,0,0],
    })
    df_doc_9 = pd.DataFrame({
        "청구차수":["1차","2차","3차"],
        "본인부담상한초과":[0,0,0],
        "청구액":[1100000,700000,600000],
        "지원금":[0,0,0],
        "장애인의료비":[0,0,0],
        "보훈청구액":[0,0,0],
        "보훈감면액":[0,0,0],
        "100/100미만보훈청구":[0,0,0],
    })
    df_claim_8 = pd.DataFrame({
        "보험유형":["건강보험","의료급여","자동차보험"],
        "입원/외래":["입원","외래","외래"],
        "본인부담상한초과":[0,0,0],
        "청구액":[900000,300000,200000],
        "지원금":[0,0,0],
        "장애인의료비":[0,0,0],
        "보훈청구액":[0,0,0],
        "보훈감면액":[0,0,0],
        "100/100미만보훈청구":[0,0,0],
    })
    df_claim_9 = pd.DataFrame({
        "보험유형":["건강보험","의료급여","자동차보험"],
        "입원/외래":["입원","외래","외래"],
        "본인부담상한초과":[0,0,0],
        "청구액":[950000,350000,150000],
        "지원금":[0,0,0],
        "장애인의료비":[0,0,0],
        "보훈청구액":[0,0,0],
        "보훈감면액":[0,0,0],
        "100/100미만보훈청구":[0,0,0],
    })

    st.session_state["dummy"] = {
        "doctor": {8:[df_doc_8], 9:[df_doc_9]},
        "claim":  {8:[df_claim_8], 9:[df_claim_9]},
    }

# ---- 전역 가드 ----
try:
    app()
except Exception as e:
    import streamlit as st, traceback
    st.error("🚨 전역 예외가 발생했습니다. 아래 스택을 확인하세요.")
    st.exception(e)
    st.code("\n".join(traceback.format_exc().splitlines()[-20:]), language="python")

