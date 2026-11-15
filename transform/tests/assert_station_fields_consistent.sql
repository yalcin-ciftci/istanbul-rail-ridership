select row_key, station_code, station_name
from {{ ref('stg_rail_station_daily') }}
where station_code is null and station_name is not null
