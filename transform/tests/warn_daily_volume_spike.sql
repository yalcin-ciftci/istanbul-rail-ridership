{{ config(severity='warn') }}

select ride_date, scope, line_name, journey_count, journey_count_estimated,
    baseline_journeys, baseline_weeks, ratio_to_baseline, ratio_estimated_to_baseline
from {{ ref('int_daily_volume_anomaly') }}
where anomaly = 'spike'
