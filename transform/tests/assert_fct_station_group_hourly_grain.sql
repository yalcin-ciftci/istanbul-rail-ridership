select ride_date, ride_hour, line_code, station_group, count(*) as row_count
from {{ ref('fct_station_group_hourly') }}
group by ride_date, ride_hour, line_code, station_group
having ride_date is null or ride_hour is null or line_code is null
    or station_group is null or count(*) <> 1
