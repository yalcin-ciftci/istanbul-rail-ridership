{{ config(severity='warn') }}

select distinct f.station_code
from {{ ref('fct_station_daily') }} as f
left join {{ ref('dim_station') }} as d on f.station_code = d.station_code
where d.station_name is null
