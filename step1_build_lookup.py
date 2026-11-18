import duckdb
import pandas as pd

ST_LUKES_DB = "st_lukes_hospital_bethlehem_campus-c43ea_parsed.duckdb"
LVHN_DB = "lehigh_valley_hospital_cedar_crest-9591f_parsed.duckdb"

def classify_procedure(cpt: str, ms_drg: str, rc: str, desc: str) -> str:
    cpt_s = str(cpt or "").strip()
    drg_s = str(ms_drg or "").strip()
    rc_s = str(rc or "").strip().zfill(4) if rc else ""
    desc_l = str(desc or "").lower()

    # 1. Emergency Room (ER)
    if (cpt_s.isdigit() and 99281 <= int(cpt_s) <= 99285) or (rc_s.isdigit() and 450 <= int(rc_s) <= 459):
        return "Emergency Room (ER)"
    
    # 2. Clinic / Office Visits
    if (cpt_s.isdigit() and 99202 <= int(cpt_s) <= 99215) or (rc_s.isdigit() and 510 <= int(rc_s) <= 529):
        return "Clinic / Outpatient Visit"
    
    # 3. Imaging / Radiology
    if (cpt_s.isdigit() and 70010 <= int(cpt_s) <= 79999) or (rc_s.isdigit() and (320 <= int(rc_s) <= 359 or 400 <= int(rc_s) <= 409 or 610 <= int(rc_s) <= 619)):
        return "Imaging / Radiology"
    
    # 4. Laboratory
    if (cpt_s.isdigit() and 80047 <= int(cpt_s) <= 89398) or (rc_s.isdigit() and 300 <= int(rc_s) <= 319):
        return "Laboratory"
    
    # 5. Orthopedics
    if (cpt_s.isdigit() and 20100 <= int(cpt_s) <= 29999) or (drg_s.isdigit() and 453 <= int(drg_s) <= 566):
        return "Orthopedics"
    
    # 6. Heart / Cardiology
    if (cpt_s.isdigit() and (33016 <= int(cpt_s) <= 37799 or 92920 <= int(cpt_s) <= 93799)) or (drg_s.isdigit() and 215 <= int(drg_s) <= 316):
        return "Heart / Cardiology"
    
    # 7. Maternity / OB-GYN
    if (cpt_s.isdigit() and 59000 <= int(cpt_s) <= 59899) or (drg_s.isdigit() and 765 <= int(drg_s) <= 807):
        return "Maternity"
    
    # 8. Surgery (General / Other Surgical CPT)
    if cpt_s.isdigit() and 10004 <= int(cpt_s) <= 69990:
        return "Surgery / Procedures"
    
    # 9. Medicine / Therapy / Injections
    if (cpt_s.isdigit() and 90281 <= int(cpt_s) <= 99607) or any(k in desc_l for k in ["physical therapy", "rehab", "infusion"]):
        return "Medicine & Therapy"
    
    return "Other / Unclassified"

def build_lookup_table():
    print("[*] Extracting unique billing codes from both DuckDB databases...")
    query = """
        SELECT DISTINCT
            COALESCE(NULLIF(TRIM(cpt), ''), NULLIF(TRIM(hcpcs), ''), NULLIF(TRIM(ms_drg), ''), NULLIF(TRIM(rc), '')) AS billing_code,
            CASE
                WHEN NULLIF(TRIM(cpt), '') IS NOT NULL THEN 'CPT'
                WHEN NULLIF(TRIM(hcpcs), '') IS NOT NULL THEN 'HCPCS'
                WHEN NULLIF(TRIM(ms_drg), '') IS NOT NULL THEN 'MS-DRG'
                ELSE 'RC'
            END AS code_type,
            NULLIF(TRIM(cpt), '') AS cpt,
            NULLIF(TRIM(ms_drg), '') AS ms_drg,
            NULLIF(TRIM(rc), '') AS rc,
            FIRST(description) AS sample_description
        FROM standard_charges
        WHERE COALESCE(NULLIF(TRIM(cpt), ''), NULLIF(TRIM(hcpcs), ''), NULLIF(TRIM(ms_drg), ''), NULLIF(TRIM(rc), '')) IS NOT NULL
        GROUP BY 1, 2, 3, 4, 5
    """
    con_sl = duckdb.connect(ST_LUKES_DB, read_only=True)
    con_lvhn = duckdb.connect(LVHN_DB, read_only=True)
    
    df_codes = pd.concat([
        con_sl.execute(query).fetchdf(),
        con_lvhn.execute(query).fetchdf()
    ], ignore_index=True).drop_duplicates(subset=['billing_code', 'code_type']).reset_index(drop=True)
    
    df_codes['care_category'] = df_codes.apply(
        lambda r: classify_procedure(r['cpt'], r['ms_drg'], r['rc'], r['sample_description']), axis=1
    )
    
    summary = df_codes.groupby(['care_category', 'code_type']).size().reset_index(name='unique_codes_count')
    print("\n[=== CARE CATEGORIES SUMMARY FOR APPROVAL ===]")
    print(summary.to_string(index=False))
    
    df_codes.to_csv("care_category_lookup.csv", index=False, encoding='utf-8-sig')
    print(f"\n[OK] Successfully saved {len(df_codes)} unique codes to care_category_lookup.csv")
    
    con_sl.close()
    con_lvhn.close()

if __name__ == "__main__":
    build_lookup_table()