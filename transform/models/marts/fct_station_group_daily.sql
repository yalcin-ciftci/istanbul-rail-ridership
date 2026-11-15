select
    f.ride_date,
    d.station_group,
    cast(sum(f.journey_count) as bigint) as journey_count,
    cast(sum(f.journey_count_estimated) as bigint) as journey_count_estimated,
    cast(sum(f.passenger_count) as bigint) as passenger_count,
    bool_or(f.has_suspect_journeys) as has_suspect_journeys,
    cast(count(distinct f.line_name) as bigint) as line_count
from {{ ref('fct_station_daily') }} as f
inner join {{ ref('dim_station') }} as d on f.station_code = d.station_code
where d.station_group is not null
group by f.ride_date, d.station_group
