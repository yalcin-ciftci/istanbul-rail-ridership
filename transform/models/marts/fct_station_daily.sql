select
    ride_date,
    station_code,
    line_name,
    cast(sum(journey_count) as bigint) as journey_count,
    cast(sum(passenger_count) as bigint) as passenger_count,
    cast(count(*) as bigint) as entrance_rows,
    cast(sum(journey_count_estimated) as bigint) as journey_count_estimated,
    bool_or(journey_suspect) as has_suspect_journeys
from {{ ref('int_rail_entrance_daily') }}
where not is_line_level
group by ride_date, station_code, line_name
