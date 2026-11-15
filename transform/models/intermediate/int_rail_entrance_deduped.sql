select
    row_key,
    source_row_id,
    resource_id,
    ride_date,
    line_name,
    station_code,
    station_name,
    is_line_level,
    district,
    longitude,
    latitude,
    journey_count,
    passenger_count,
    source_last_modified,
    ingested_at
from {{ ref('stg_rail_station_daily') }}
qualify row_number() over (
    partition by
        resource_id, ride_date, line_name, station_code, station_name, district,
        longitude, latitude, journey_count, passenger_count
    order by source_row_id
) = 1
