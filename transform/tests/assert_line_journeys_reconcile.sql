with entrance_totals as (
    select year(ride_date) as ride_year, sum(journey_count) as journey_count,
        sum(journey_count_estimated) as journey_count_estimated
    from {{ ref('int_rail_entrance_daily') }}
    group by year(ride_date)
), line_totals as (
    select year(ride_date) as ride_year, sum(journey_count) as journey_count,
        sum(journey_count_estimated) as journey_count_estimated
    from {{ ref('fct_line_daily') }}
    group by year(ride_date)
)
select
    coalesce(e.ride_year, l.ride_year) as ride_year,
    e.journey_count as entrance_journeys,
    l.journey_count as line_journeys,
    e.journey_count_estimated as entrance_journeys_estimated,
    l.journey_count_estimated as line_journeys_estimated
from entrance_totals as e
full outer join line_totals as l on e.ride_year = l.ride_year
where e.journey_count is distinct from l.journey_count
    or e.journey_count_estimated is distinct from l.journey_count_estimated
