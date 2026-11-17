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
    q.ride_date,
    q.row_count,
    q.even_share,
    q.journey_passenger_ratio,
    (q.even_share >= 0.99 and q.journey_passenger_ratio >= 1.8)
        or o.ride_date is not null as journey_suspect,
    case
        when q.even_share >= 0.99 and q.journey_passenger_ratio >= 1.8 then 'rule'
        when o.ride_date is not null then 'hourly_override'
    end as journey_suspect_source
from daily_quality as q
left join {{ ref('journey_suspect_overrides') }} as o on q.ride_date = o.ride_date
