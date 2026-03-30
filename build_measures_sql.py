PREFIXES = ["ccg", "pcn", "stp"]

base_cols = ["month", "numerator", "denominator", "percentile"]

org_id_col = {
    "ccg": "pct_id",
    "pcn": "pcn_id",
    "stp": "stp_id",
}

def build_sql(measures_df, existing_tables):
    parts = []

    for _, row in measures_df.iterrows():
        measure_id = row["measure_id"]
        if not measure_id:
            continue

        for prefix in PREFIXES:
            table_name = f"{prefix}_data_{measure_id}"
            if table_name not in existing_tables:
                continue

            source_col = org_id_col[prefix]
            select_sql = ", ".join([
                *base_cols,
                f"{source_col} AS org_id",
                f"'{prefix}' AS org_type",
                f"'{measure_id}' AS measure",
            ])

            parts.append(
                f"SELECT {select_sql} FROM `ebmdatalab.measures.{table_name}`"
            )

    return "\nUNION ALL\n".join(parts)