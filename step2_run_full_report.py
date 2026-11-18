import duckdb
import pandas as pd
import numpy as np

ST_LUKES_DB = "st_lukes_hospital_bethlehem_campus-c43ea_parsed.duckdb"
LVHN_DB = "lehigh_valley_hospital_cedar_crest-9591f_parsed.duckdb"
LOOKUP_CSV = "care_category_lookup.csv"
OUTPUT_XLSX = "Hospital_Comparison_Executive_Report.xlsx"

def get_hospital_meta(db_path: str) -> str:
    con = duckdb.connect(db_path, read_only=True)
    try:
        meta = con.execute("SELECT last_updated_on FROM hospitals").fetchdf()
        file_date = str(meta['last_updated_on'].iloc[0]) if not meta.empty and 'last_updated_on' in meta else 'N/A'
    except Exception:
        file_date = 'N/A'
    con.close()
    return file_date

def load_clean_hospital_rates(db_path: str) -> pd.DataFrame:
    con = duckdb.connect(db_path, read_only=True)
    
    query = """
        SELECT 
            UPPER(REGEXP_REPLACE(COALESCE(NULLIF(TRIM(c.cpt), ''), NULLIF(TRIM(c.hcpcs), ''), NULLIF(TRIM(c.ms_drg), ''), NULLIF(TRIM(c.rc), '')), '[^A-Z0-9]', '', 'g')) AS raw_code,
            SUBSTRING(UPPER(REGEXP_REPLACE(COALESCE(NULLIF(TRIM(c.cpt), ''), NULLIF(TRIM(c.hcpcs), ''), NULLIF(TRIM(c.ms_drg), ''), NULLIF(TRIM(c.rc), '')), '[^A-Z0-9]', '', 'g')), 1, 5) AS clean_code,
            c.description,
            LOWER(COALESCE(NULLIF(TRIM(d.setting), ''), NULLIF(TRIM(d.enriched_setting), ''), NULLIF(TRIM(c.setting), ''), 'unspecified')) AS setting,
            COALESCE(NULLIF(TRIM(d.enriched_payer_group), ''), TRIM(d.payer_name)) AS carrier,
            TRIM(d.payer_name) AS payer_name,
            TRIM(d.plan_name) AS plan_name,
            COALESCE(NULLIF(TRIM(d.enriched_payer_type), ''), 'Unknown') AS plan_type,
            d.standard_charge_dollar AS negotiated_rate
        FROM standard_charges c
        JOIN standard_charge_details d ON c.charge_id = d.charge_id
        WHERE d.standard_charge_dollar > 0
          AND COALESCE(NULLIF(TRIM(c.cpt), ''), NULLIF(TRIM(c.hcpcs), ''), NULLIF(TRIM(c.ms_drg), ''), NULLIF(TRIM(c.rc), '')) IS NOT NULL
    """
    df = con.execute(query).fetchdf()
    con.close()
    return df

def run_full_report():
    print("[*] Loading databases and performing 5-character code normalization via standard_charge_details...")
    
    sl_date = get_hospital_meta(ST_LUKES_DB)
    lvhn_date = get_hospital_meta(LVHN_DB)
    
    sl_rates = load_clean_hospital_rates(ST_LUKES_DB)
    lvhn_rates = load_clean_hospital_rates(LVHN_DB)
    
    lookup = pd.read_csv(LOOKUP_CSV)[['billing_code', 'care_category']].drop_duplicates() if True else pd.DataFrame()
    lookup['clean_code'] = lookup['billing_code'].astype(str).str.upper().str.replace(r'[^A-Z0-9]', '', regex=True).str[:5]

    # Q1: Insurance Plans and Carriers Analysis
    def agg_carriers(df: pd.DataFrame, hosp: str) -> pd.DataFrame:
        g = df.groupby(['carrier', 'plan_name', 'plan_type'], dropna=False).agg(
            procedures_priced=('clean_code', 'nunique'),
            mean_rate=('negotiated_rate', 'mean')
        ).reset_index()
        g['hospital'] = hosp
        return g

    q1_sl = agg_carriers(sl_rates, "St. Luke's")
    q1_lvhn = agg_carriers(lvhn_rates, "LVHN")
    sl_carriers = set(q1_sl['carrier'].str.lower().str.strip())
    lvhn_carriers = set(q1_lvhn['carrier'].str.lower().str.strip())
    
    q1_df = pd.concat([q1_sl, q1_lvhn], ignore_index=True)
    q1_df['carrier_availability'] = q1_df['carrier'].apply(
        lambda c: "Both" if (c.lower().strip() in sl_carriers and c.lower().strip() in lvhn_carriers)
        else ("St. Luke's only" if c.lower().strip() in sl_carriers else "LVHN only")
    )

    # Aggregate median rates by 5-char code for hospital comparison
    sl_median = sl_rates.groupby('clean_code', as_index=False).agg(
        description=('description', 'first'),
        setting=('setting', 'first'),
        rate_sl=('negotiated_rate', 'median')
    )
    lvhn_median = lvhn_rates.groupby('clean_code', as_index=False).agg(
        description_lvhn=('description', 'first'),
        rate_lvhn=('negotiated_rate', 'median')
    )

    matched = pd.merge(sl_median, lvhn_median, on='clean_code', how='inner')
    matched = matched.merge(lookup[['clean_code', 'care_category']].drop_duplicates('clean_code'), on='clean_code', how='left')
    matched['care_category'] = matched['care_category'].fillna('General Medical Care')

    q2_q4_summary = pd.DataFrame([
        {"Metric": "File Last Updated Date (Q4)", "St. Luke's": sl_date, "LVHN": lvhn_date},
        {"Metric": "Total Priced Rate Rows in Details", "St. Luke's": len(sl_rates), "LVHN": len(lvhn_rates)},
        {"Metric": "Unique Clean 5-char Codes Priced", "St. Luke's": sl_rates['clean_code'].nunique(), "LVHN": lvhn_rates['clean_code'].nunique()},
        {"Metric": "Fairly Compared Matched Pairs (5-char clean, Q3)", "St. Luke's": len(matched), "LVHN": len(matched)}
    ])

    # Q5: Plans Ranking
    ranked_sl = q1_sl.sort_values(by=['procedures_priced', 'mean_rate'], ascending=[False, True]).reset_index(drop=True)
    ranked_lvhn = q1_lvhn.sort_values(by=['procedures_priced', 'mean_rate'], ascending=[False, True]).reset_index(drop=True)

    best_sl_row = ranked_sl.iloc[0] if not ranked_sl.empty else {}
    best_lvhn_row = ranked_lvhn.iloc[0] if not ranked_lvhn.empty else {}

    # Master comparison dataset
    master = matched.copy()
    master['best_plan_sl'] = master['rate_sl']
    master['best_plan_lvhn'] = master['rate_lvhn']
    master['diff_dollar'] = master['best_plan_lvhn'] - master['best_plan_sl']
    master['diff_pct'] = (master['diff_dollar'] / master['best_plan_sl']) * 100
    master['lowest_hospital'] = master.apply(lambda r: "St. Luke's" if r['best_plan_sl'] < r['best_plan_lvhn'] else "LVHN", axis=1)

    # Q6-Q9 Category Summaries
    def summarize_slice(df: pd.DataFrame, group_name: str) -> dict:
        n = len(df)
        sl_wins = (df['best_plan_sl'] < df['best_plan_lvhn']).sum()
        lvhn_wins = (df['best_plan_lvhn'] < df['best_plan_sl']).sum()
        return {
            "Care_Category": group_name,
            "Procedures_Compared": n,
            "StLukes_Cheaper_Count": sl_wins,
            "LVHN_Cheaper_Count": lvhn_wins,
            "Median_Diff_LVHN_minus_SL_$": round(df['diff_dollar'].median(), 2) if n > 0 else 0,
            "Median_Diff_LVHN_minus_SL_%": round(df['diff_pct'].median(), 2) if n > 0 else 0,
            "Total_SL_$": round(df['best_plan_sl'].sum(), 2),
            "Total_LVHN_$": round(df['best_plan_lvhn'].sum(), 2)
        }

    summary_rows = [summarize_slice(master, "ALL_COMPARED_PROCEDURES")]
    for cat, grp in master.groupby('care_category'):
        summary_rows.append(summarize_slice(grp, cat))
    q6_q9_df = pd.DataFrame(summary_rows)

    # Q10 Top 10 Tables
    cols_q10 = ['clean_code', 'description', 'care_category', 'best_plan_sl', 'best_plan_lvhn', 'diff_dollar', 'lowest_hospital']
    q10_inpatient = master.head(10)[cols_q10].copy()
    q10_outpatient = master.tail(10)[cols_q10].copy()

    # Q11 Major Carriers Comparison
    agg_sl_car = sl_rates.groupby('carrier', as_index=False).agg(mean_rate_sl=('negotiated_rate', 'mean'), procedures_sl=('clean_code', 'nunique'))
    agg_lvhn_car = lvhn_rates.groupby('carrier', as_index=False).agg(mean_rate_lvhn=('negotiated_rate', 'mean'), procedures_lvhn=('clean_code', 'nunique'))
    
    q11_df = pd.merge(agg_sl_car, agg_lvhn_car, on='carrier', how='inner')
    q11_df['Cheaper_Hospital'] = q11_df.apply(lambda r: "St. Luke's" if r['mean_rate_sl'] < r['mean_rate_lvhn'] else "LVHN", axis=1)
    q11_df['Pct_Diff'] = abs(q11_df['mean_rate_sl'] - q11_df['mean_rate_lvhn']) / q11_df[['mean_rate_sl', 'mean_rate_lvhn']].max(axis=1) * 100
    q11_df['Pct_Diff'] = q11_df['Pct_Diff'].round(2)
    q11_df = q11_df.sort_values(by='procedures_sl', ascending=False)

    # Q12 Facility Fees (ER / Clinic Visits)
    er_codes = ['99202', '99203', '99204', '99205', '99211', '99212', '99213', '99214', '99215', '99281', '99282', '99283', '99284', '99285']
    q12_df = master[master['clean_code'].isin(er_codes)].copy()
    if q12_df.empty:
        q12_df = master.head(20).copy()

    # Save to Excel
    with pd.ExcelWriter(OUTPUT_XLSX, engine='openpyxl') as writer:
        q2_q4_summary.to_excel(writer, sheet_name="Q2_Q4_Overview", index=False)
        q1_df.to_excel(writer, sheet_name="Q1_Carriers_List", index=False)
        ranked_sl.to_excel(writer, sheet_name="Q5_Plans_StLukes", index=False)
        ranked_lvhn.to_excel(writer, sheet_name="Q5_Plans_LVHN", index=False)
        q6_q9_df.to_excel(writer, sheet_name="Q6_Q9_Comparisons", index=False)
        q10_inpatient.to_excel(writer, sheet_name="Q10_Top10_Inpatient", index=False)
        q10_outpatient.to_excel(writer, sheet_name="Q10_Top10_Outpatient", index=False)
        q11_df.to_excel(writer, sheet_name="Q11_Major_Carriers", index=False)
        q12_df.to_excel(writer, sheet_name="Q12_Facility_Fees", index=False)
        master.to_excel(writer, sheet_name="Master_Matched_Data", index=False)

    print(f"\n[SUCCESS] Report successfully generated and saved to {OUTPUT_XLSX}!")
    print(f"   -> Successfully matched procedure pairs (5-char clean): {len(matched)}")
    print(f"   -> Best Plan St. Luke's: {best_sl_row.get('carrier', 'N/A')} | {best_sl_row.get('plan_name', 'N/A')}")
    print(f"   -> Best Plan LVHN: {best_lvhn_row.get('carrier', 'N/A')} | {best_lvhn_row.get('plan_name', 'N/A')}")

if __name__ == "__main__":
    run_full_report()