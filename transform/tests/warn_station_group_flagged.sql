{{ config(severity='warn') }}

select station_code, station_name, station_group, note
from {{ ref('station_group_map') }}
where status = 'flagged'
