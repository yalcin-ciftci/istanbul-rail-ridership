select ride_date, station_code, line_name, count(*) as row_count
from {{ ref('fct_station_daily') }}
group by ride_date, station_code, line_name
having count(*) > 1
