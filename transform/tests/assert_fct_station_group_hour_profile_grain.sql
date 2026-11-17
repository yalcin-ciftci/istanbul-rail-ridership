select station_group, day_type, ride_hour, count(*) as row_count
from {{ ref('fct_station_group_hour_profile') }}
group by station_group, day_type, ride_hour
having station_group is null or day_type is null or ride_hour is null or count(*) <> 1
