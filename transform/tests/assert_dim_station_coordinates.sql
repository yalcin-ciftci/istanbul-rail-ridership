select station_code, longitude, latitude
from {{ ref('dim_station') }}
where longitude not between 27.9 and 29.95
    or latitude not between 40.7 and 41.6
    or (longitude is null and latitude is not null)
    or (longitude is not null and latitude is null)
