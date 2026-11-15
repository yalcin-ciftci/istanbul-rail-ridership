{{ config(severity='warn') }}

select row_key, station_code, station_name
from {{ ref('stg_rail_station_daily') }}
where station_code is not null and station_name is null
