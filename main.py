# Write a more defensive v2 of the Streamlit app with richer error handling,
# filename-safe Excel reads, and column auto-normalization/fallbacks.
from pathlib import Path

v2 = r'''# streamlit_claim_compare_app_v2.py
# -*- coding: utf-8 -*-
# -------------------------------------------------------------
# 스트림릿 앱: EDI 청구통계 월별 비교 (의사별 / 보험유형 / 입원·외래) — v2 (안정화)
# 변경점
#  - 업로드 파일을 BytesIO로 읽어 FileNotFoundError 등 I/O 문제 방지
#  - 컬럼 자동 정규화(공백 제거/대소문자/동의어 매핑: 보험구분→보험유형, 입/외→입원/외래 등)
#  - 예외 발생 시 앱에서 상세 스택을 그대로 표시(st.exception) + 디버그 로그 섹션 추가
#  - 시연용 더미데이터 생성 버튼 제공
# -------------------------------------------------------------

import io
import re
from typing import Dict, List, Tuple, Optional

import pandas as pd
import streamlit as st

st.set_page_config(page_title="EDI 청구통계 월별 비교 (안정화 v2)", layout="wide")

LOGS: List[str] = []

def log(msg: str):
    LOGS.append(str(msg))

# ============ 상단 설명 ============
with st.expander("📌 사용 안내 (필독)", expanded=True):
    st.markdown(
        """
        **설명 1 — 파일명 규칙 (EDI · 청구통계):**
        - 진료의 기준: 파일명은 반드시 **`의사별_8월`** 과 같은 형식으로 지정하세요.  
        - 보험/입·외/보훈 등 구분 기준: 파일명은 **`청구_8월`** 또는 **`청구별_8월`** 형식으로 지정하세요.
        
        **설명 2 — 업로드 및 비교 로직:**
        - 여러 개의 XLSX 파일을 **동시에 업로드** 할 수 있습니다.
        - 업로드된 파일들 중 **가장 큰 '월' 값**을 **당월**로, 그 **직전 월**을 **전달**로 자동 인식합니다.
        - 집계 기준:
            - 의사별 파일 → **`청구차수`** 별 비교
            - 청구/청구별 파일 → **`보험유형`** 별, **`입원/외래`** 별 비교(두 표 제공)
        - 합산 대상 컬럼(아래 7개를 모두 더한 금액을 **'청구액'** 으로 간주):
            - `본인부담상한초과`, `청구액`, `지원금`, `장애인의료비`, `보훈청구액`, `보훈감면액`, `100/100미만보훈청구`
        - **증감 표기 규칙** *(요청사항 준수)*:
            - 증가(+): `▼`, 감소(-): `▲`, 변동없음: `—`
        """
    )

# ============ 업로더/더미 데이터 ============
st.subheader("📤 XLSX 업로드")
uploaded_files = st.file_uploader(
    "여러 개 파일을 동시에 올릴 수 있습니다. (확장자: .xlsx)",
    type=["xlsx"],
    accept_multiple_files=True,
)

if st.button("🎯 테스트용 더미데이터로 시연하기"):
    # 8월/9월 더미(의사별/청구) 생성
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
    st.success("더미데이터를 적재했습니다. 아래에서 바로 비교를 실행해 보세요.")

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
    if ("청구별" in name) or ("청구" in name) or ("claim" in nm):
        return "claim"
    return None

# 동의어/표기 변형 매핑
RENAME_CANDIDATES = {
    "보험유형": ["보험유형","보험 구분","보험구분","보험-구분","보험_구분","보험종류"],
    "입원/외래": ["입원/외래","입/외","입원외래","입원_외래","입원 · 외래","입원-외래"],
    "청구차수": ["청구차수","청구 차수","청구_차수","청구-차수"],
    # 합산 컬럼 후보들(동의어/철자 변형 포함 가능시 추가)
    "본인부담상한초과":["본인부담상한초과","본인부담 상한초과","본인부담-상한초과"],
    "청구액":["청구액","총청구액","청구 금액","청구-금액"],
    "지원금":["지원금","지원 금액"],
    "장애인의료비":["장애인의료비","장애인 의료비"],
    "보훈청구액":["보훈청구액","보훈 청구액"],
    "보훈감면액":["보훈감면액","보훈 감면액"],
    "100/100미만보훈청구":["100/100미만보훈청구","100/100 미만 보훈청구","100/100미만 보훈청구"],
}

def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    # 우선 공백/개행 제거
    df = df.copy()
    newcols = {}
    cols_stripped = [str(c).strip() for c in df.columns]
    df.columns = cols_stripped

    # 동의어 매핑
    used = set()
    for target, aliases in RENAME_CANDIDATES.items():
        for al in aliases:
            if al in df.columns and target not in df.columns:
                newcols[al] = target
                used.add(al)
                break
    if newcols:
        df = df.rename(columns=newcols)
        log(f"[정규화] 컬럼 이름 매핑: {newcols}")

    return df

SUM_COLS = ["본인부담상한초과","청구액","지원금","장애인의료비","보훈청구액","보훈감면액","100/100미만보훈청구"]

def _coerce_numeric(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s.astype(str).str.replace(",", "", regex=False), errors="coerce").fillna(0)

def _prepare_df(df: pd.DataFrame) -> pd.DataFrame:
    df = normalize_columns(df)
    miss = [c for c in SUM_COLS if c not in df.columns]
    for c in miss:
        df[c] = 0
    for c in SUM_COLS:
        df[c] = _coerce_numeric(df[c])
    df["__합산청구액__"] = df[SUM_COLS].sum(axis=1)
    return df

def _group_sum(df: pd.DataFrame, by_col: str) -> pd.DataFrame:
    if by_col not in df.columns:
        work = df.copy()
        work[by_col] = "미지정"
        df = work
        st.warning(f"'{by_col}' 컬럼이 없어 임시값 '미지정'으로 집계합니다.")
    g = df.groupby(by_col, dropna=False)["__합산청구액__"].sum().reset_index()
    g = g.rename(columns={by_col: "구분", "__합산청구액__": "청구액"})
    return g

def _compare(prev_df: pd.DataFrame, curr_df: pd.DataFrame) -> pd.DataFrame:
    merged = pd.merge(prev_df, curr_df, on="구분", how="outer", suffixes=("_전달", "_당월")).fillna(0)
    merged["증감"] = merged["청구액_당월"] - merged["청구액_전달"]
    def fmt(delta: float) -> str:
        if delta > 0:
            return f"▼{int(abs(delta)):,}"
        if delta < 0:
            return f"▲{int(abs(delta)):,}"
        return "—"
    merged["증감(기호)"] = merged["증감"].apply(fmt)
    merged = merged[["구분","청구액_전달","청구액_당월","증감(기호)","증감"]]
    merged = merged.sort_values("구분").reset_index(drop=True)
    return merged

def _concat_same_month(dfs: List[pd.DataFrame]) -> pd.DataFrame:
    if not dfs:
        return pd.DataFrame()
    return pd.concat(dfs, ignore_index=True)

def _read_xlsx_uploaded(uploaded) -> pd.DataFrame:
    # 업로더 객체를 안전하게 BytesIO로 변환하여 읽기
    # 여러 번 read() 방지용으로 항상 한 번만 호출하는 함수 안에서 처리
    data = uploaded.read()
    bio = io.BytesIO(data)
    df = pd.read_excel(bio, sheet_name=0, dtype=str, engine="openpyxl")
    return df

# ======== 데이터 적재 ========
buckets: Dict[str, Dict[int, List[pd.DataFrame]]] = {"doctor": {}, "claim": {}}

if "dummy" in st.session_state:
    # 더미 데이터가 있으면 우선 반영
    for kind in ("doctor","claim"):
        for m, dfs in st.session_state["dummy"][kind].items():
            buckets.setdefault(kind, {}).setdefault(m, []).extend(dfs)
    log("더미 데이터 버킷 합류 완료.")

if uploaded_files:
    for upl in uploaded_files:
        try:
            name = upl.name
            kind = _detect_kind(name)
            month = _parse_month_from_name(name)
            if not kind or not month:
                log(f"무시됨(종류/월 판단 불가): {name}")
                st.warning(f"무시됨: `{name}` (종류/월 판단 불가)")
                continue

            df_raw = _read_xlsx_uploaded(upl)
            df = _prepare_df(df_raw)
            buckets.setdefault(kind, {}).setdefault(month, []).append(df)
            log(f"인식됨: {name} → {kind}/{month}월, rows={len(df)}")

        except Exception as e:
            st.exception(e)
            log(f"[오류] 파일 읽기 실패: {upl.name} → {e}")

# ======== 처리/뷰 ========
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
        prev_candidates = [m for m in doctor_months if m < curr_doctor]
        prev_doctor = max(prev_candidates) if prev_candidates else None
        if prev_doctor is None:
            st.warning("의사별 전달 파일을 찾지 못했습니다. (예: 당월이 9월이면 8월 파일 필요)")
        else:
            st.caption(f"전달 인식: **{prev_doctor}월**")

        if st.button("의사별 비교 실행", type="primary"):
            try:
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
            except Exception as e:
                st.exception(e)
                log(f"[오류] 의사별 비교 실행 실패: {e}")

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
                try:
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
                except Exception as e:
                    st.exception(e)
                    log(f"[오류] 보험유형 비교 실행 실패: {e}")

        with col22:
            if st.button("입원·외래 기준 비교 실행", key="run_claim_io"):
                try:
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
                except Exception as e:
                    st.exception(e)
                    log(f"[오류] 입원·외래 비교 실행 실패: {e}")

# ============ 다운로드 ============
st.markdown("---")
st.subheader("📥 엑셀로 내보내기")

if (
    ("out_doc" in st.session_state)
    or ("out_ins" in st.session_state)
    or ("out_io" in st.session_state)
):
    try:
        outbuf = io.BytesIO()
        with pd.ExcelWriter(outbuf, engine="openpyxl") as xw:
            if "out_doc" in st.session_state:
                prev_m, curr_m = st.session_state.get("out_doc_months", (None, None))
                st.session_state["out_doc"].to_excel(xw, sheet_name=f"의사별({prev_m}→{curr_m})", index=False)
            if "out_ins" in st.session_state:
                prev_m, curr_m = st.session_state.get("out_ins_months", (None, None))
                st.session_state["out_ins"].to_excel(xw, sheet_name=f"보험유형({prev_m}→{curr_m})", index=False)
            if "out_io" in st.session_state:
                prev_m, curr_m = st.session_state.get("out_io_months", (None, None))
                st.session_state["out_io"].to_excel(xw, sheet_name=f"입원외래({prev_m}→{curr_m})", index=False)
        outbuf.seek(0)
        st.download_button(
            "⬇️ 비교 결과 엑셀 다운로드",
            data=outbuf,
            file_name="청구통계_월별비교_결과.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    except Exception as e:
        st.exception(e)
        log(f"[오류] 엑셀 내보내기 실패: {e}")
else:
    st.info("먼저 상단의 비교 실행 버튼을 눌러 결과를 생성하세요.")

# ============ 디버그 로그 ============
with st.expander("🪵 디버그 로그", expanded=False):
    if LOGS:
        st.write("\n".join(LOGS))
    else:
        st.caption("로그가 없습니다.")
'''
Path("/mnt/data/streamlit_claim_compare_app_v2.py").write_text(v2, encoding="utf-8")
print("Wrote /mnt/data/streamlit_claim_compare_app_v2.py")

