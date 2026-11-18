with hourly_day as (
    select ride_date, line_code, sum(passage_count) as passage_count
    from {{ ref('fct_station_group_hourly') }}
    -- Günlük veri 2021-01-01'de başlar; öncesi kapsama boşluğu değildir.
    where ride_date >= date '2021-01-01' and ride_date < date '2024-11-01'
    group by ride_date, line_code
), hourly_month as (
    select h.line_code, l.daily_line_name,
        strftime(h.ride_date, '%Y-%m') as month,
        count(*) filter (where h.passage_count > 0) as hourly_days,
        sum(h.passage_count) as hourly_passage
    from hourly_day as h
    inner join {{ ref('hourly_line_map') }} as l on h.line_code = l.line_code
    where nullif(l.daily_line_name, '') is not null
    group by h.line_code, l.daily_line_name, strftime(h.ride_date, '%Y-%m')
), daily_month as (
    select line_name, strftime(ride_date, '%Y-%m') as month,
        count(distinct ride_date) filter (where journey_count_estimated > 0) as daily_days,
        sum(journey_count_estimated) as daily_journey_estimated
    from {{ ref('fct_line_daily') }}
    where ride_date < date '2024-11-01'
    group by line_name, strftime(ride_date, '%Y-%m')
), joined as (
    select h.line_code, h.daily_line_name, h.month, h.hourly_days, h.hourly_passage,
        coalesce(d.daily_days, 0) as daily_days,
        coalesce(d.daily_journey_estimated, 0) as daily_journey_estimated,
        coalesce(a.quality_flag, e.quality_flag) as hourly_month_flag
    from hourly_month as h
    left join daily_month as d on h.daily_line_name = d.line_name and h.month = d.month
    left join {{ ref('fct_hourly_coverage_month') }} as a
        on h.month = a.month and a.line_code = 'ALL'
    left join {{ ref('fct_hourly_coverage_month') }} as e
        on h.month = e.month and e.line_code = '*' and a.month is null
)
select line_code, daily_line_name, month, hourly_days, hourly_passage,
    daily_days, daily_journey_estimated, hourly_month_flag,
    case
        when hourly_month_flag is distinct from 'ok' then 'hourly_incomplete'
        when hourly_days >= 20 and daily_days = 0 then 'missing_in_daily'
        when hourly_days >= 20 and daily_days < hourly_days / 2.0 then 'partial_in_daily'
        else 'ok'
    end as status
from joined
