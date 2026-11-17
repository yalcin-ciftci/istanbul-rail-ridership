{{ config(severity='warn') }}

select station_group, sum(hourly_passage) as hourly_passage,
    sum(daily_journey_count) as daily_journey_count,
    sum(hourly_passage) / nullif(sum(daily_journey_count), 0) as ratio_to_source
from {{ ref('fct_hourly_daily_reconciliation') }}
where hourly_month_flag = 'ok' and not has_suspect_journeys
    and hourly_passage is not null and hourly_passage <> 0
    and daily_journey_count is not null and daily_journey_count <> 0
group by station_group
having sum(daily_journey_count) >= 100000
    and (sum(hourly_passage) / nullif(sum(daily_journey_count), 0) < 0.95
        or sum(hourly_passage) / nullif(sum(daily_journey_count), 0) > 1.05)
