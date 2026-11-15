with line_totals as (
    select year(ride_date) as ride_year, sum(journey_count) as journey_count
    from {{ ref('fct_line_daily') }}
    group by year(ride_date)
), line_level_totals as (
    select year(ride_date) as ride_year, sum(journey_count) as journey_count
    from {{ ref('int_rail_entrance_daily') }}
    where is_line_level
    group by year(ride_date)
), station_part as (
    select l.ride_year, l.journey_count - coalesce(ll.journey_count, 0) as journey_count
    from line_totals as l
    left join line_level_totals as ll on l.ride_year = ll.ride_year
), station_totals as (
    select year(ride_date) as ride_year, sum(journey_count) as journey_count
    from {{ ref('fct_station_daily') }}
    group by year(ride_date)
)
select
    coalesce(l.ride_year, s.ride_year) as ride_year,
    l.journey_count as line_station_journeys,
    s.journey_count as station_journeys
from station_part as l
full outer join station_totals as s on l.ride_year = s.ride_year
where l.journey_count is distinct from s.journey_count
