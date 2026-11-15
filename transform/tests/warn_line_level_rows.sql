{{ config(severity='warn') }}

select row_key, line_name, station_code, station_name
from {{ ref('stg_rail_station_daily') }}
where is_line_level
