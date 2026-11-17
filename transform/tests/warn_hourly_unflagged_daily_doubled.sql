{{ config(severity='warn') }}

select ride_date, hourly_passage, daily_journey_count, ratio_to_source
from {{ ref('fct_hourly_daily_reconciliation_day') }}
where not has_suspect_journeys and status = 'daily_doubled'
