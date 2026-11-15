select row_key, longitude, latitude
from {{ ref('stg_rail_station_daily') }}
where longitude not between 27.9 and 29.95
    or latitude not between 40.7 and 41.6
    or (longitude is null and latitude is not null)
    or (longitude is not null and latitude is null)
