# This cell writes a ready-to-run Streamlit app to the shared folder so the user can download it.
# The app implements the UI and logic requested: multiple XLSX uploads, filename parsing,
# month-to-month comparison (8월 vs 9월, etc.) for 의사별/보험유형/입원·외래.
# Usage:
# 1) pip install streamlit pandas openpyxl
# 2) streamlit run streamlit_claim_compare_app.py

from pathlib import Path
app_code = r'''# streamlit_claim_compare_app.py
# -*- coding: utf-8 -*-
# -------------------------------------------------------------
# 스트림릿 앱: EDI 청구통계 월별 비교 (의사별 / 보험유형 / 입원·외래)
# 요구사항 요약
# - 화면 상단에 설명문(주석 성격) 노출
# - XLSX 다중 업로드 지원, 파일명 규칙 예:
#    * "의사별_8월.xlsx" (공백/하이픈/대소문자 변형 허용)
#    * "청구_8월.xlsx" 또는 "청구별_8월.xlsx"
# - 업로드 묶음에서 가장 최신 월(예: 9월)을 '당월', 그 직전 월(예: 8월)을 '전달'로 인식
# - 당월과 전달을 아래 기준으로 집계 후 비교
#   * 의사별: '청구차수' 단위
#   * 보험유형: '보험유형' 단위
#   * 입원·외래: '입원/외래' 단위
# - 합산 대상 컬럼(모두 더함 → '청구액'으로 간주):
#   ['본인부담상한초과','청구액','지원금','장애인의료비','보훈청구액','보훈감면액','100/100미만보훈청구']
# - 증감 기호 맵핑(요청사항 그대로 적용):
#     delta > 0  → '▼' (증가하였을 때 하향 화살표)
#     delta < 0  → '▲' (감소하였을 때 상향 화살표)
#     delta = 0  → '—'
# - 결과표에는 '전달비교' 열(전달값)과 '당월 청구액' 및 '증감(기호+절대값)' 표시
# - 엑셀 다운로드 제공 (의사별/보험유형/입원·외래 시트로 저장)
# -------------------------------------------------------------

import io
import re
import sys
import math
import warnings
from typing import Dict, List, Tuple, Optional

import pandas as pd
import streamlit as st

st.set_page_config(page_title="EDI 청구통계 월별 비교", layout="wide")

# ============ 상단 설명 (요청된 주석/설명문 그대로 노출) ============
with st.expander("📌 사용 안내 (필독)", expanded=True):
    st.markdown(
        """
        **설명 1 — 파일명 규칙 (EDI · 청구통계):**
        - 진료의 기준: 파일명은 반드시 **`의사별_8월`** 과 같은 형식으로 지정하세요.  
          *(예: `의사별_9월.xlsx`, 공백/하이픈은 허용: `의사별 - 9월.xlsx`, `의사별 9월.xlsx` 등)*
        - 보험/입·외/보훈 등 구분 기준: 파일명은 **`청구_8월`** 또는 **`청구별_8월`** 형식으로 지정하세요.
        
        **설명 2 — 업로드 및 비교 로직:**
        - 여러 개의 XLSX 파일을 **동시에 업로드** 할 수 있습니다.
        - 업로드된 파일들 중 **가장 큰 '월' 값**을 **당월**로, 그 **직전 월**을 **전달**로 자동 인식합니다.  
          예) `의사별_9월`과 `의사별_8월`을 함께 올리면 9월이 당월, 8월이 전달로 매칭됩니다.
        - 집계 기준:
            - 의사별 파일 → **`청구차수`** 별 비교
            - 청구/청구별 파일 → **`보험유형`** 별, **`입원/외래`** 별 비교(두 표 제공)
        - 합산 대상 컬럼(아래 7개를 모두 더한 금액을 **'청구액'** 으로 간주):
            - `본인부담상한초과`, `청구액`, `지원금`, `장애인의료비`, `보훈청구액`, `보훈감면액`, `100/100미만보훈청구`
        - **증감 표기 규칙** *(요청사항 준수)*:
            - 감소(음수)일 때 **`▲`**, 증가(양수)일 때 **`▼`**, 변동없음은 **`—`**
        
        **주의:** 컬럼명이 정확히 일치하지 않으면 자동으로 0으로 보정합니다. (누락 컬럼은 경고 표시)
        """
    )

# ============ 업로더 ============
st.subheader("📤 XLSX 업로드")
uploaded_files = st.file_uploader(
    "여러 개 파일을 동시에 올릴 수 있습니다. (확장자: .xlsx)",
    type=["xlsx"],
    accept_multiple_files=True,
)

# ======== 유틸 ========
MONTH_RE = re.compile(r"(?:^|[^0-9])(\d{1,2})\s*월", re.I)

def _parse_month_from_name(name: str) -> Optional[int]:
    m = MONTH_RE.search(name or "")
    if not m:
        return None
    try:
        mm = int(m.group(1))
        if 1 <= mm <= 12:
            return mm
    except:
        pass
    return None

def _detect_kind(name: str) -> Optional[str]:
    nm = (name or "").lower()
    if "의사별" in name:
        return "doctor"
    # '청구' 또는 '청구별' 모두 claim으로 처리
    if ("청구별" in name) or ("청구" in name):
        return "claim"
    # 영어/기타 변형이 있다면 여기 추가
    return None

SUM_COLS = ["본인부담상한초과","청구액","지원금","장애인의료비","보훈청구액","보훈감면액","100/100미만보훈청구"]

def _coerce_numeric(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s.astype(str).str.replace(",", "", regex=False), errors="coerce").fillna(0)

def _prepare_df(df: pd.DataFrame) -> pd.DataFrame:
    # 누락 컬럼은 0으로 생성, 숫자화
    miss = [c for c in SUM_COLS if c not in df.columns]
    for c in miss:
        df[c] = 0
    for c in SUM_COLS:
        df[c] = _coerce_numeric(df[c])
    # 합산 청구액 계산 (요청에 따라 7개 항목 모두 합산한 값을 '청구액'으로 사용)
    df["__합산청구액__"] = df[SUM_COLS].sum(axis=1)
    return df

def _group_sum(df: pd.DataFrame, by_col: str) -> pd.DataFrame:
    if by_col not in df.columns:
        # 없는 경우 공백으로 대체해서 전체 합만 나오도록 처리
        work = df.copy()
        work[by_col] = "미지정"
        df = work
        st.warning(f"'{by_col}' 컬럼이 없어 임시값 '미지정'으로 집계합니다.")
    g = df.groupby(by_col, dropna=False)["__합산청구액__"].sum().reset_index()
    g = g.rename(columns={by_col: "구분", "__합산청구액__": "청구액"})
    return g

def _compare(prev_df: pd.DataFrame, curr_df: pd.DataFrame) -> pd.DataFrame:
    # 좌우 조인 후 결측 0 보정
    merged = pd.merge(prev_df, curr_df, on="구분", how="outer", suffixes=("_전달", "_당월")).fillna(0)
    merged["증감"] = merged["청구액_당월"] - merged["청구액_전달"]
    # 요청된 화살표 규칙 적용
    def fmt(delta: float) -> str:
        if delta > 0:
            return f"▼{int(abs(delta)):,}"
        if delta < 0:
            return f"▲{int(abs(delta)):,}"
        return "—"
    merged["증감(기호)"] = merged["증감"].apply(fmt)
    # 보기 좋게 정렬
    merged = merged[["구분","청구액_전달","청구액_당월","증감(기호)","증감"]]
    merged = merged.sort_values("구분").reset_index(drop=True)
    return merged

def _concat_same_month(dfs: List[pd.DataFrame]) -> pd.DataFrame:
    if not dfs:
        return pd.DataFrame()
    return pd.concat(dfs, ignore_index=True)

# ======== 메인 처리 ========
if uploaded_files:
    # 파일들을 타입(kind) · 월(month)로 버킷팅
    buckets: Dict[str, Dict[int, List[pd.DataFrame]]] = {"doctor": {}, "claim": {}}
    details: List[str] = []

    for upl in uploaded_files:
        name = upl.name
        kind = _detect_kind(name)
        month = _parse_month_from_name(name)
        if not kind or not month:
            details.append(f"❌ 무시됨: `{name}` (종류/월 판단 불가)")
            continue

        try:
            # 첫 시트만 사용 (필요 시 수정)
            df = pd.read_excel(upl, sheet_name=0, dtype=str)
            df = _prepare_df(df)
            buckets.setdefault(kind, {}).setdefault(month, []).append(df)
            details.append(f"✅ 인식됨: `{name}` → 종류: **{kind}**, 월: **{month}월**, 행수: {len(df):,}")
        except Exception as e:
            details.append(f"⚠️ 오류: `{name}` 읽기 실패 → {e}")

    with st.expander("업로드 인식 결과 (로그)", expanded=False):
        st.write("\n\n".join(details))

    # 각 종류별로 사용 가능한 월 도출
    doctor_months = sorted(buckets["doctor"].keys())
    claim_months  = sorted(buckets["claim"].keys())

    col1, col2 = st.columns(2)
    with col1:
        st.markdown("### 🩺 의사별 (파일명: `의사별_○월`)")
        if not doctor_months:
            st.info("의사별 파일이 없습니다.")
        else:
            curr_default = max(doctor_months)
            curr_doctor = st.selectbox("의사별 당월 선택", doctor_months, index=doctor_months.index(curr_default), key="sel_doc_curr")
            # 전달 후보: 당월보다 작은 월 중 가장 큰 값, 없으면 자동 감지 실패
            prev_candidates = [m for m in doctor_months if m < curr_doctor]
            prev_doctor = max(prev_candidates) if prev_candidates else None
            if prev_doctor is None:
                st.warning("의사별 전달 파일을 찾지 못했습니다. (예: 당월이 9월이면 8월 파일 필요)")
            else:
                st.caption(f"전달 인식: **{prev_doctor}월**")

            if st.button("의사별 비교 실행", type="primary"):
                prev_df = _concat_same_month(buckets["doctor"].get(prev_doctor, []))
                curr_df = _concat_same_month(buckets["doctor"].get(curr_doctor, []))

                if prev_df.empty or curr_df.empty:
                    st.error("의사별 비교를 위한 월 데이터가 부족합니다.")
                else:
                    prev_g = _group_sum(prev_df, "청구차수")
                    curr_g = _group_sum(curr_df, "청구차수")
                    out_doc = _compare(prev_g, curr_g)

                    st.markdown("#### 결과표 — 의사별(청구차수)")
                    st.dataframe(
                        out_doc.style.format({
                            "청구액_전달": "{:,.0f}",
                            "청구액_당월": "{:,.0f}",
                            "증감": "{:,.0f}",
                        }),
                        use_container_width=True,
                    )
                    st.session_state["out_doc"] = out_doc
                    st.session_state["out_doc_months"] = (prev_doctor, curr_doctor)

    with col2:
        st.markdown("### 📊 청구 기준 (파일명: `청구_○월` 또는 `청구별_○월`)")
        if not claim_months:
            st.info("청구/청구별 파일이 없습니다.")
        else:
            curr_default = max(claim_months)
            curr_claim = st.selectbox("청구 기준 당월 선택", claim_months, index=claim_months.index(curr_default), key="sel_claim_curr")
            prev_candidates = [m for m in claim_months if m < curr_claim]
            prev_claim = max(prev_candidates) if prev_candidates else None
            if prev_claim is None:
                st.warning("청구 기준 전달 파일을 찾지 못했습니다. (예: 당월이 9월이면 8월 파일 필요)")
            else:
                st.caption(f"전달 인식: **{prev_claim}월**")

            col21, col22 = st.columns(2)
            with col21:
                if st.button("보험유형 기준 비교 실행", key="run_claim_ins"):
                    prev_df = _concat_same_month(buckets["claim"].get(prev_claim, []))
                    curr_df = _concat_same_month(buckets["claim"].get(curr_claim, []))
                    if prev_df.empty or curr_df.empty:
                        st.error("비교를 위한 월 데이터가 부족합니다.")
                    else:
                        prev_g = _group_sum(prev_df, "보험유형")
                        curr_g = _group_sum(curr_df, "보험유형")
                        out_ins = _compare(prev_g, curr_g)
                        st.markdown("#### 결과표 — 보험유형")
                        st.dataframe(
                            out_ins.style.format({
                                "청구액_전달": "{:,.0f}",
                                "청구액_당월": "{:,.0f}",
                                "증감": "{:,.0f}",
                            }),
                            use_container_width=True,
                        )
                        st.session_state["out_ins"] = out_ins
                        st.session_state["out_ins_months"] = (prev_claim, curr_claim)

            with col22:
                if st.button("입원·외래 기준 비교 실행", key="run_claim_io"):
                    prev_df = _concat_same_month(buckets["claim"].get(prev_claim, []))
                    curr_df = _concat_same_month(buckets["claim"].get(curr_claim, []))
                    if prev_df.empty or curr_df.empty:
                        st.error("비교를 위한 월 데이터가 부족합니다.")
                    else:
                        prev_g = _group_sum(prev_df, "입원/외래")
                        curr_g = _group_sum(curr_df, "입원/외래")
                        out_io = _compare(prev_g, curr_g)
                        st.markdown("#### 결과표 — 입원·외래")
                        st.dataframe(
                            out_io.style.format({
                                "청구액_전달": "{:,.0f}",
                                "청구액_당월": "{:,.0f}",
                                "증감": "{:,.0f}",
                            }),
                            use_container_width=True,
                        )
                        st.session_state["out_io"] = out_io
                        st.session_state["out_io_months"] = (prev_claim, curr_claim)

    # ============ 다운로드 묶음 ============
    st.markdown("---")
    st.subheader("📥 엑셀로 내보내기")

    if (
        ("out_doc" in st.session_state)
        or ("out_ins" in st.session_state)
        or ("out_io" in st.session_state)
    ):
        # 파일명: 요청 예시 반영 → 당월 월 기준으로 구성
        outbuf = io.BytesIO()
        with pd.ExcelWriter(outbuf, engine="openpyxl") as xw:
            if "out_doc" in st.session_state:
                prev_m, curr_m = st.session_state.get("out_doc_months", (None, None))
                df = st.session_state["out_doc"].copy()
                df.to_excel(xw, sheet_name=f"의사별({prev_m}→{curr_m})", index=False)

            if "out_ins" in st.session_state:
                prev_m, curr_m = st.session_state.get("out_ins_months", (None, None))
                df = st.session_state["out_ins"].copy()
                df.to_excel(xw, sheet_name=f"보험유형({prev_m}→{curr_m})", index=False)

            if "out_io" in st.session_state:
                prev_m, curr_m = st.session_state.get("out_io_months", (None, None))
                df = st.session_state["out_io"].copy()
                df.to_excel(xw, sheet_name=f"입원외래({prev_m}→{curr_m})", index=False)

        outbuf.seek(0)
        st.download_button(
            "⬇️ 비교 결과 엑셀 다운로드",
            data=outbuf,
            file_name="청구통계_월별비교_결과.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    else:
        st.info("먼저 상단의 비교 실행 버튼을 눌러 결과를 생성하세요.")

else:
    st.info("XLSX 파일을 업로드하면 자동으로 종류/월을 인식합니다. ('의사별_○월', '청구(별)_○월')")
'''
Path("/mnt/data/streamlit_claim_compare_app.py").write_text(app_code, encoding="utf-8")
print("Wrote /mnt/data/streamlit_claim_compare_app.py")
