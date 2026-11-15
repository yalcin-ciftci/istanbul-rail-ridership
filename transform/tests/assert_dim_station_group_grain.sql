select station_group, count(*) as row_count
from {{ ref('dim_station_group') }}
group by station_group
having station_group is null or count(*) <> 1
