"""
表格顯示格式化共用模組。
"""

import pandas as pd


def style_rows_by_sign(df: pd.DataFrame, col_name: str, pos_color: str = "#f8c6c6", neg_color: str = "#50C878"):
    """依 col_name 欄位正負只該欄位上色(正紅負綠，0不上色)，並將數值欄位格式化為整數0位或2位小數，回傳可直接傳入 st.dataframe() 的 pandas Styler"""
    if df.empty or col_name not in df.columns:
        return df

    def _fmt(val):
        if pd.isna(val):
            return ""
        if float(val).is_integer():
            return f"{val:.0f}"
        return f"{val:.2f}"

    def _col_style(s):
        styles = []
        for val in s:
            if pd.isna(val) or val == 0:
                styles.append("")
            else:
                styles.append(f"background-color: {pos_color if val > 0 else neg_color}")
        return styles

    numeric_cols = df.select_dtypes(include="number").columns
    fmt = {c: _fmt for c in numeric_cols}
    return df.style.apply(_col_style, subset=[col_name]).format(fmt)