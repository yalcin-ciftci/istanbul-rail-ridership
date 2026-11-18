{{ config(materialized='table') }}

with anomalies as (
    select
        ride_date, scope, line_name, journey_count, journey_count_estimated,
        has_suspect_journeys, baseline_journeys, baseline_weeks,
        ratio_to_baseline, ratio_estimated_to_baseline, anomaly
    from {{ ref('int_daily_volume_anomaly') }}
), calendar as (
    select
        ride_date, event_types, labels, is_holiday, is_half_day_holiday,
        is_covid_restriction, is_partial_covid_restriction, has_special_event
    from {{ ref('int_calendar_day') }}
), dates as (
    select distinct ride_date from anomalies
), baseline as (
    select
        dates.ride_date,
        cast(count(*) filter (where calendar.is_covid_restriction) as integer)
            as baseline_restricted_days
    from dates
    cross join range(1, 9) as weeks(week_offset)
    left join calendar
        on calendar.ride_date = dates.ride_date - cast(7 * weeks.week_offset as integer)
    group by dates.ride_date
)
select
    a.ride_date, a.scope, a.line_name, a.journey_count, a.journey_count_estimated,
    a.has_suspect_journeys, a.baseline_journeys, a.baseline_weeks,
    a.ratio_to_baseline, a.ratio_estimated_to_baseline, a.anomaly,
    c.event_types as calendar_event_types,
    c.labels as calendar_labels,
    b.baseline_restricted_days,
    case
        when a.anomaly not in ('spike', 'drop') then null
        when a.anomaly = 'drop'
            and c.is_covid_restriction
            then 'covid_restriction'
        when a.anomaly = 'drop' and (c.is_holiday or c.is_half_day_holiday)
            then 'holiday'
        when a.anomaly = 'spike' and c.has_special_event then 'special_event'
        when a.anomaly = 'spike' and b.baseline_restricted_days >= 1
            then 'baseline_restricted'
        else 'unexplained'
    end as calendar_explanation
from anomalies as a
left join calendar as c on a.ride_date = c.ride_date
left join baseline as b on a.ride_date = b.ride_date
