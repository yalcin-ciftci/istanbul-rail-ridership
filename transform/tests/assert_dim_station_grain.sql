select station_code, count(*) as row_count
from {{ ref('dim_station') }}
group by station_code
having count(*) > 1
