with hourly_daily as (
    select ride_date, station_group, sum(passage_count) as hourly_passage
    from {{ ref('fct_station_group_hourly') }}
    where ride_date between date '2021-01-01' and date '2024-10-31'
    group by ride_date, station_group
), daily as (
    select ride_date, station_group, journey_count, journey_count_estimated,
        has_suspect_journeys
    from {{ ref('fct_station_group_daily') }}
    where ride_date between date '2021-01-01' and date '2024-10-31'
), joined as (
    select
        coalesce(h.ride_date, d.ride_date) as ride_date,
        coalesce(h.station_group, d.station_group) as station_group,
        strftime(coalesce(h.ride_date, d.ride_date), '%Y-%m') as month,
        h.hourly_passage,
        d.journey_count as daily_journey_count,
        d.journey_count_estimated as daily_journey_estimated,
        d.has_suspect_journeys,
        h.hourly_passage / nullif(d.journey_count, 0) as ratio_to_source,
        h.hourly_passage / nullif(d.journey_count_estimated, 0) as ratio_to_estimate
    from hourly_daily as h
    full outer join daily as d
        on h.ride_date = d.ride_date and h.station_group = d.station_group
), flagged as (
    select j.ride_date, j.station_group, j.month, j.hourly_passage,
        j.daily_journey_count, j.daily_journey_estimated, j.has_suspect_journeys,
        j.ratio_to_source, j.ratio_to_estimate,
        coalesce(a.quality_flag, e.quality_flag) as hourly_month_flag
    from joined as j
    left join {{ ref('fct_hourly_coverage_month') }} as a
        on j.month = a.month and a.line_code = 'ALL'
    left join {{ ref('fct_hourly_coverage_month') }} as e
        on j.month = e.month and e.line_code = '*' and a.month is null
)
select ride_date, station_group, month, hourly_passage, daily_journey_count,
    daily_journey_estimated, has_suspect_journeys, ratio_to_source,
    ratio_to_estimate, hourly_month_flag,
    case
        when hourly_passage is null or hourly_passage = 0 then 'only_daily'
        when daily_journey_count is null or daily_journey_count = 0 then 'only_hourly'
        when hourly_month_flag is distinct from 'ok' then 'hourly_incomplete'
        when ratio_to_estimate between 0.97 and 1.03 then 'match'
        when ratio_to_source between 0.47 and 0.53 then 'daily_doubled'
        else 'mismatch'
    end as status
from flagged
