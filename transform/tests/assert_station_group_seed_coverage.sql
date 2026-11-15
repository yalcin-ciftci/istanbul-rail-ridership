select d.station_code
from {{ ref('dim_station') }} as d
left join {{ ref('station_group_map') }} as g on d.station_code = g.station_code
where g.station_code is null
