select row_key, journey_count, passenger_count
from {{ ref('stg_rail_station_daily') }}
where journey_count < 0
    or passenger_count < 0
    or passenger_count > journey_count
