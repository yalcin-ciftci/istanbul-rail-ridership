select line_code, station_key, count(*) as row_count
from {{ ref('hourly_station_map') }}
group by line_code, station_key
having line_code is null or station_key is null or count(*) <> 1
