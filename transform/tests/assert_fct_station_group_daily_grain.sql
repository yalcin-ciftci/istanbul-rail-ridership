select ride_date, station_group, count(*) as row_count
from {{ ref('fct_station_group_daily') }}
group by ride_date, station_group
having ride_date is null or station_group is null or count(*) <> 1
