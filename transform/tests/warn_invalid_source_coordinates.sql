{{ config(severity='warn') }}

select resource_id, _id, longitude, latitude
from {{ source('raw', 'rail_station_daily') }}
where (longitude <> '' and not regexp_full_match(longitude, '^[0-9.]*[0-9][0-9.]*$'))
    or (latitude <> '' and not regexp_full_match(latitude, '^[0-9.]*[0-9][0-9.]*$'))
