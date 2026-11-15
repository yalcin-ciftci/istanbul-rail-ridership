with daily_quality as (
    select
        ride_date,
        count(*) as row_count,
        count(*) filter (where journey_count % 2 = 0) * 1.0 / count(*) as even_share,
        sum(journey_count) * 1.0 / nullif(sum(passenger_count), 0) as journey_passenger_ratio
    from {{ ref('int_rail_entrance_deduped') }}
    group by ride_date
)
select
    ride_date,
    row_count,
    even_share,
    journey_passenger_ratio,
    even_share >= 0.99 and journey_passenger_ratio >= 1.8 as journey_suspect
from daily_quality
