{{ config(severity='warn') }}

select line_code, daily_line_name, month, hourly_days, hourly_passage,
    daily_days, daily_journey_estimated, hourly_month_flag, status
from {{ ref('fct_line_daily_coverage_month') }}
where status in ('missing_in_daily', 'partial_in_daily')
