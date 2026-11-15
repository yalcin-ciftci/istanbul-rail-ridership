with station_totals as (
    select year(ride_date) as ride_year, sum(journey_count) as journey_count
    from {{ ref('fct_station_daily') }}
    group by year(ride_date)
), group_totals as (
    select year(ride_date) as ride_year, sum(journey_count) as journey_count
    from {{ ref('fct_station_group_daily') }}
    group by year(ride_date)
), ungrouped_totals as (
    select year(f.ride_date) as ride_year, sum(f.journey_count) as journey_count
    from {{ ref('fct_station_daily') }} as f
    left join {{ ref('dim_station') }} as d on f.station_code = d.station_code
    where d.station_group is null
    group by year(f.ride_date)
), accounted_totals as (
    select
        coalesce(g.ride_year, u.ride_year) as ride_year,
        coalesce(g.journey_count, 0) + coalesce(u.journey_count, 0) as journey_count
    from group_totals as g
    full outer join ungrouped_totals as u on g.ride_year = u.ride_year
)
select
    coalesce(s.ride_year, a.ride_year) as ride_year,
    s.journey_count as station_journeys,
    a.journey_count as accounted_journeys
from station_totals as s
full outer join accounted_totals as a on s.ride_year = a.ride_year
where s.journey_count is distinct from a.journey_count
