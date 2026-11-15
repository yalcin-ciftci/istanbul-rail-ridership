{{ config(severity='warn') }}

select s.row_key, s.ride_date, s.source_row_id
from {{ ref('stg_rail_station_daily') }} as s
left join {{ ref('int_rail_entrance_deduped') }} as i on s.row_key = i.row_key
where i.row_key is null
