{{ config(severity='warn') }}

select year, line_code, daily_line_name, report_value, daily_journey_estimated,
    ratio_estimated, gap_lower_bound, ratio_with_gap, status
from {{ ref('int_external_reconciliation_line_year') }}
where status in ('low', 'high', 'missing_in_daily')
    and (status <> 'missing_in_daily' or gap_lower_bound = 0)
