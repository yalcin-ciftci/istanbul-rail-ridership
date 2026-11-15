{{ config(severity='warn') }}

select station_code, station_name
from {{ ref('dim_station') }}
where station_group is null
