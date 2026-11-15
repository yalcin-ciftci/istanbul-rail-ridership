select
    ride_date,
    line_name,
    cast(sum(journey_count) as bigint) as journey_count,
    cast(sum(passenger_count) as bigint) as passenger_count,
    cast(count(distinct station_code) as bigint) as station_count,
    bool_or(is_line_level) as has_line_level_rows,
    cast(sum(journey_count_estimated) as bigint) as journey_count_estimated,
    bool_or(journey_suspect) as has_suspect_journeys
from {{ ref('int_rail_entrance_daily') }}
group by ride_date, line_name
