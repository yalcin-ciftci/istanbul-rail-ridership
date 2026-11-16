-- Mapped methods must name a group; unmapped/ambiguous rows must not.
select line_code, station_key, match_method, station_group
from {{ ref('hourly_station_map') }}
where (match_method in ('auto', 'auto_line', 'manual')) <> (nullif(station_group, '') is not null)
