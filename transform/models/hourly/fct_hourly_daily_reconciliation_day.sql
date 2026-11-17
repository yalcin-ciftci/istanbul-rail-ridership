with classified as (
    select ride_date, month, hourly_passage, daily_journey_count,
        daily_journey_estimated, has_suspect_journeys, hourly_month_flag, status,
        hourly_passage is not null and hourly_passage <> 0
            and daily_journey_count is not null and daily_journey_count <> 0 as both_present
    from {{ ref('fct_hourly_daily_reconciliation') }}
), totals as (
    select ride_date, month,
        count(*) filter (where both_present) as groups_both,
        count(*) filter (where status = 'only_daily') as groups_only_daily,
        count(*) filter (where status = 'only_hourly') as groups_only_hourly,
        sum(hourly_passage) filter (where both_present) as hourly_passage,
        sum(daily_journey_count) filter (where both_present) as daily_journey_count,
        sum(daily_journey_estimated) filter (where both_present) as daily_journey_estimated,
        bool_or(has_suspect_journeys) as has_suspect_journeys,
        max(hourly_month_flag) as hourly_month_flag
    from classified
    group by ride_date, month
), ratios as (
    select ride_date, month, groups_both, groups_only_daily, groups_only_hourly,
        hourly_passage, daily_journey_count, daily_journey_estimated,
        has_suspect_journeys,
        hourly_passage / nullif(daily_journey_count, 0) as ratio_to_source,
        hourly_passage / nullif(daily_journey_estimated, 0) as ratio_to_estimate,
        hourly_month_flag
    from totals
)
select ride_date, month, groups_both, groups_only_daily, groups_only_hourly,
    hourly_passage, daily_journey_count, daily_journey_estimated,
    has_suspect_journeys, ratio_to_source, ratio_to_estimate, hourly_month_flag,
    case
        when groups_both = 0 then 'no_overlap'
        when hourly_month_flag is distinct from 'ok' then 'hourly_incomplete'
        when ratio_to_estimate between 0.97 and 1.03 then 'match'
        when ratio_to_source between 0.47 and 0.53 then 'daily_doubled'
        else 'mismatch'
    end as status
from ratios
