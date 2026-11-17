{{ config(severity='warn') }}

select ride_date, hourly_passage, daily_journey_estimated, ratio_to_estimate
from {{ ref('fct_hourly_daily_reconciliation_day') }}
where has_suspect_journeys and hourly_month_flag = 'ok'
    and (ratio_to_estimate < 0.97 or ratio_to_estimate > 1.03)
