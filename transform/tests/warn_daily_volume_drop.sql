{{ config(severity='warn') }}

select ride_date, scope, line_name, journey_count, journey_count_estimated,
    baseline_journeys, baseline_weeks, ratio_to_baseline, ratio_estimated_to_baseline,
    calendar_labels, baseline_restricted_days
from {{ ref('int_daily_volume_anomaly_calendar') }}
where anomaly = 'drop'
    and calendar_explanation = 'unexplained'
