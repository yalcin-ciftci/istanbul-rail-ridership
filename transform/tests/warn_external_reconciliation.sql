{{ config(severity='warn') }}

select year, operator, report_value, daily_journey_estimated, daily_journey_count,
    ratio_estimated, ratio_source, gap_lower_bound, ratio_with_gap, source_url, source_page
from {{ ref('int_external_reconciliation_year') }}
where ratio_with_gap not between 0.97 and 1.03
