# streamlit_claim_compare_app_v4.py
# -*- coding: utf-8 -*-
# -------------------------------------------------------------
# 스트림릿 앱 v4 — 요구사항 정렬판
#  - 주석(설명) 그대로 노출
#  - XLSX 다중 업로드, 파일명에서 월 인식(1~12월)
#  - 최신월(당월)과 직전월(전달) 자동 매칭
#  - 집계 기준
#     * 의사별 → '과목구분'
#     * 청구/청구별(보험) → '보험구분'
#     * 청구/청구별(입원외래) → '입원외래'
#  - 합산 컬럼 7개 모두 더해 '청구액'으로 간주
#  - 증감 기호: (-)→▼, (+)→▲, 0→—
#  - 예외/진단 로그 표시
# -------------------------------------------------------------

import io
import re
from typing import Dict, List, Optional

import pandas as pd
import streamlit as st

st.set_page_config(page_title="EDI 청구통계 월별 비교 (v4)", layout="wide")

# ------------------- 상단 설명(요청된 주석) -------------------
with st.expander("📌 화면 설명 (필독)", expanded=True):
    st.markdown(
        """
        **<주석> 화면에 설명문 넣어주기**  
        * 설명 = **EDI -  청구통계 - 진료의**,  파일명은 **'의사별_8월'** 이렇게 지정해야함  
        * 설명 = **EDI -  청구통계 - 보험구분(+), 입/외(+), 보훈등구분(+)**, 파일명은 **'청구_8월'** 이렇게 지정해야함
        
        ---
        - xlsx 파일은 여러 개 올릴 수 있습니다.  
        - 파일명을 비교하여 넣을 때 1월~12월 모든 가지수를 고려합니다.  
        - 예: **의사별_9월 + 의사별_8월** → 9월(당월), 8월(전달)
        
        **비교 규칙**
        1) 의사별:
           - 당월: **'의사별_9월'**의 데이터를 **'과목구분'** 별로 합산하여 '청구액'으로 사용  
           - 전달: **'의사별_8월'** 동일 집계 후 **전달비교** 칸으로 표시  
           - 합산 항목: `본인부담상한초과 + 청구액 + 지원금 + 장애인의료비 + 보훈청구액 + 보훈감면액 + 100/100미만보훈청구`  
           - 증감 표기: **감소(-)→▼**, **증가(+)→▲**
        
        2) 청구(보험구분):
           - 당월: **'청구별_9월'**(또는 '청구_9월')을 **'보험구분'** 별로 합산하여 '청구액'으로 사용  
           - 전달: **'청구별_8월'**(또는 '청구_8월') 동일 집계 후 비교  
           - 증감 표기: **감소(-)→▼**, **증가(+)→▲**
        
        3) 청구(입원외래):
           - 당월: **'청구별_9월'**(또는 '청구_9월')을 **'입원외래'** 별로 합산하여 '청구액'으로 사용  
           - 전달: **'청구별_8월'**(또는 '청구_8월') 동일 집계 후 비교  
           - 증감 표기: **감소(-)→▼**, **증가(+)→▲**
        """
    )

# ------------------- 업로드 -------------------
st.subheader("📤 XLSX 업로드")
uploaded_files = st.file_uploader(
    "여러 개 파일을 동시에 올릴 수 있습니다. (확장자: .xlsx)",
    type=["xlsx"],
    accept_multiple_files=True,
)

# ------------------- 유틸 -------------------
MONTH_RE = re.compile(r"(?:^|[^0-9])(\d{1,2})\s*월", re.I)

def parse_month(name: str) -> Optional[int]:
    m = MONTH_RE.search(name or "")
    if not m:
        return None
    try:
        mm = int(m.group(1))
        return mm if 1 <= mm <= 12 else None
    except:
        return None

def detect_kind(name: str) -> Optional[str]:
    """의사별 / 청구(또는 청구별) 구분"""
    if "의사별" in name:
        return "doctor"
    low = name.lower()
    if ("청구별" in name) or ("청구" in name) or ("claim" in low):
        return "claim"
    return None

# 동의어/표기 변형 매핑
RENAME = {
    # 집계 기준 열
    "과목구분": ["과목구분","과목","과","진료과","진료과목","진료과 구분","과코드","진료과코드"],
    "보험구분": ["보험구분","보험유형","보험 구분","보험-구분","보험_구분","보험종류"],
    "입원외래": ["입원외래","입원/외래","입/외","입외","입원-외래","입원_외래","입원 • 외래","입원 · 외래"],
    # 합산 대상 열
    "본인부담상한초과": ["본인부담상한초과","본인부담 상한초과","본인부담-상한초과"],
    "청구액": ["청구액","총청구액","청구 금액","청구-금액"],
    "지원금": ["지원금","지원 금액"],
    "장애인의료비": ["장애인의료비","장애인 의료비"],
    "보훈청구액": ["보훈청구액","보훈 청구액"],
    "보훈감면액": ["보훈감면액","보훈 감면액"],
    "100/100미만보훈청구": ["100/100미만보훈청구","100/100 미만 보훈청구","100/100미만 보훈청구"],
}

SUM_COLS = ["본인부담상한초과","청구액","지원금","장애인의료비","보훈청구액","보훈감면액","100/100미만보훈청구"]

def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]
    # 일치하지 않는 이름 매핑
    mapping = {}
    for target, aliases in RENAME.items():
        if target in df.columns:
            continue
        for al in aliases:
            if al in df.columns:
                mapping[al] = target
                break
    if mapping:
        df = df.rename(columns=mapping)
        st.caption(f"🧭 컬럼 매핑: {mapping}")
    return df

def to_num(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s.astype(str).str.replace(",", "", regex=False), errors="coerce").fillna(0)

def prepare_df(df: pd.DataFrame) -> pd.DataFrame:
    df = normalize_columns(df)
    for c in SUM_COLS:
        if c not in df.columns:
            df[c] = 0
    for c in SUM_COLS:
        df[c] = to_num(df[c])
    df["__합산청구액__"] = df[SUM_COLS].sum(axis=1)
    return df

def group_sum(df: pd.DataFrame, by_col: str) -> pd.DataFrame:
    if by_col not in df.columns:
        tmp = df.copy()
        tmp[by_col] = "미지정"
        st.warning(f"'{by_col}' 컬럼이 없어 임시값 '미지정'으로 집계합니다.")
        df = tmp
    g = df.groupby(by_col, dropna=False)["__합산청구액__"].sum().reset_index()
    g = g.rename(columns={by_col: "구분", "__합산청구액__": "청구액"})
    return g

def compare(prev_df: pd.DataFrame, curr_df: pd.DataFrame) -> pd.DataFrame:
    merged = pd.merge(prev_df, curr_df, on="구분", how="outer", suffixes=("_전달","_당월")).fillna(0)
    merged["증감"] = merged["청구액_당월"] - merged["청구액_전달"]
    def mark(x):
        if x > 0: return f"▲{int(abs(x)):,}"
        if x < 0: return f"▼{int(abs(x)):,}"
        return "—"
    merged["증감(기호)"] = merged["증감"].apply(mark)
    cols = ["구분","청구액_전달","청구액_당월","증감(기호)","증감"]
    return merged[cols].sort_values("구분").reset_index(drop=True)

def read_xlsx(uploaded) -> pd.DataFrame:
    raw = uploaded.read()
    if len(raw) < 4:
        raise ValueError(f"{uploaded.name}: 파일이 비정상적으로 작습니다.")
    # 간단한 XLSX 서명 검사
    if raw[:2] != b"PK":
        raise ValueError(f"{uploaded.name}: XLSX 형식이 아닐 수 있습니다. (엑셀에서 .xlsx로 다시 저장 후 업로드)")
    bio = io.BytesIO(raw)
    return pd.read_excel(bio, sheet_name=0, dtype=str, engine="openpyxl")

def cat(dfs: List[pd.DataFrame]) -> pd.DataFrame:
    return pd.concat(dfs, ignore_index=True) if dfs else pd.DataFrame()

# ------------------- 적재: kind/month 버킷 -------------------
buckets: Dict[str, Dict[int, List[pd.DataFrame]]] = {"doctor": {}, "claim": {}}
log_lines: List[str] = []

if uploaded_files:
    for upl in uploaded_files:
        try:
            name = upl.name
            kind = detect_kind(name)
            mm = parse_month(name)
            if not kind or not mm:
                st.warning(f"무시됨: `{name}` (종류/월 인식 실패)")
                log_lines.append(f"무시: {name} kind={kind} mm={mm}")
                continue
            df_raw = read_xlsx(upl)
            df = prepare_df(df_raw)
            buckets.setdefault(kind, {}).setdefault(mm, []).append(df)
            log_lines.append(f"인식: {name} → {kind}/{mm}월 rows={len(df)}")
        except Exception as e:
            st.exception(e)
            log_lines.append(f"[오류] {upl.name}: {e}")

if log_lines:
    with st.expander("🪵 업로드 로그", expanded=False):
        st.code("\n".join(log_lines))

# ------------------- 비교 실행 UI -------------------
st.markdown("---")
c1, c2 = st.columns(2)

# 1) 의사별(과목구분)
with c1:
    st.markdown("### 🩺 의사별 (집계: **과목구분**)")
    doc_months = sorted(buckets["doctor"].keys())
    if not doc_months:
        st.info("의사별 파일(의사별_○월)이 없습니다.")
    else:
        curr = max(doc_months)
        prev_candidates = [m for m in doc_months if m < curr]
        prev = max(prev_candidates) if prev_candidates else None
        st.caption(f"자동 인식 → 당월: **{curr}월**, 전달: **{prev or '없음'}**")
        if st.button("의사별 비교 실행", type="primary"):
            try:
                prev_df = cat(buckets["doctor"].get(prev, []))
                curr_df = cat(buckets["doctor"].get(curr, []))
                if prev_df.empty or curr_df.empty:
                    st.error("의사별 비교에 필요한 월 데이터가 부족합니다.")
                else:
                    out_prev = group_sum(prev_df, "과목구분")
                    out_curr = group_sum(curr_df, "과목구분")
                    out = compare(out_prev, out_curr)
                    st.markdown("#### 결과표 — 의사별(과목구분)")
                    st.dataframe(
                        out.style.format({
                            "청구액_전달":"{:,.0f}",
                            "청구액_당월":"{:,.0f}",
                            "증감":"{:,.0f}",
                        }),
                        use_container_width=True,
                    )
                    st.session_state["out_doc"] = out
                    st.session_state["out_doc_months"] = (prev, curr)
            except Exception as e:
                st.exception(e)

# 2) 청구: 보험구분 / 입원외래
with c2:
    st.markdown("### 📊 청구 기준 (집계: **보험구분 / 입원외래**)")
    claim_months = sorted(buckets["claim"].keys())
    if not claim_months:
        st.info("청구/청구별 파일(청구_○월, 청구별_○월)이 없습니다.")
    else:
        curr = max(claim_months)
        prev_candidates = [m for m in claim_months if m < curr]
        prev = max(prev_candidates) if prev_candidates else None
        st.caption(f"자동 인식 → 당월: **{curr}월**, 전달: **{prev or '없음'}**")
        cc1, cc2 = st.columns(2)
        with cc1:
            if st.button("보험구분 기준 비교 실행"):
                try:
                    prev_df = cat(buckets["claim"].get(prev, []))
                    curr_df = cat(buckets["claim"].get(curr, []))
                    if prev_df.empty or curr_df.empty:
                        st.error("비교에 필요한 월 데이터가 부족합니다.")
                    else:
                        out_prev = group_sum(prev_df, "보험구분")
                        out_curr = group_sum(curr_df, "보험구분")
                        out = compare(out_prev, out_curr)
                        st.markdown("#### 결과표 — 보험구분")
                        st.dataframe(
                            out.style.format({
                                "청구액_전달":"{:,.0f}",
                                "청구액_당월":"{:,.0f}",
                                "증감":"{:,.0f}",
                            }),
                            use_container_width=True,
                        )
                        st.session_state["out_ins"] = out
                        st.session_state["out_ins_months"] = (prev, curr)
                except Exception as e:
                    st.exception(e)
        with cc2:
            if st.button("입원외래 기준 비교 실행"):
                try:
                    prev_df = cat(buckets["claim"].get(prev, []))
                    curr_df = cat(buckets["claim"].get(curr, []))
                    if prev_df.empty or curr_df.empty:
                        st.error("비교에 필요한 월 데이터가 부족합니다.")
                    else:
                        out_prev = group_sum(prev_df, "입원외래")
                        out_curr = group_sum(curr_df, "입원외래")
                        out = compare(out_prev, out_curr)
                        st.markdown("#### 결과표 — 입원외래")
                        st.dataframe(
                            out.style.format({
                                "청구액_전달":"{:,.0f}",
                                "청구액_당월":"{:,.0f}",
                                "증감":"{:,.0f}",
                            }),
                            use_container_width=True,
                        )
                        st.session_state["out_io"] = out
                        st.session_state["out_io_months"] = (prev, curr)
                except Exception as e:
                    st.exception(e)

# ------------------- 엑셀 다운로드 -------------------
st.markdown("---")
st.subheader("📥 엑셀로 내보내기")
try:
    if (
        ("out_doc" in st.session_state)
        or ("out_ins" in st.session_state)
        or ("out_io" in st.session_state)
    ):
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as xw:
            if "out_doc" in st.session_state:
                pm, cm = st.session_state.get("out_doc_months",(None,None))
                st.session_state["out_doc"].to_excel(xw, sheet_name=f"의사별({pm}→{cm})", index=False)
            if "out_ins" in st.session_state:
                pm, cm = st.session_state.get("out_ins_months",(None,None))
                st.session_state["out_ins"].to_excel(xw, sheet_name=f"보험구분({pm}→{cm})", index=False)
            if "out_io" in st.session_state:
                pm, cm = st.session_state.get("out_io_months",(None,None))
                st.session_state["out_io"].to_excel(xw, sheet_name=f"입원외래({pm}→{cm})", index=False)
        buf.seek(0)
        st.download_button(
            "⬇️ 비교 결과 엑셀 다운로드",
            data=buf,
            file_name="청구통계_월별비교_결과.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    else:
        st.info("먼저 상단에서 비교 실행 버튼을 눌러 결과를 생성하세요.")
except Exception as e:
    st.exception(e)
    st.exception(e)
    st.code("\n".join(traceback.format_exc().splitlines()[-20:]), language="python")

