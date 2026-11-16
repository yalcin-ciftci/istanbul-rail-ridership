select
    h.ride_date,
    h.ride_hour,
    h.line_code,
    m.station_group,
    cast(sum(h.passage_count) as bigint) as passage_count,
    cast(sum(h.passenger_count) as bigint) as passenger_count,
    cast(count(*) as bigint) as source_rows
from {{ ref('stg_rail_hourly') }} as h
inner join {{ ref('hourly_station_map') }} as m
    on h.line_code = m.line_code and h.station_key = m.station_key
where nullif(m.station_group, '') is not null
group by h.ride_date, h.ride_hour, h.line_code, m.station_group
