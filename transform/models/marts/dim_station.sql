with station_rows as (
    select
        station_code, station_name, district, longitude, latitude,
        ride_date, source_row_id, line_name
    from {{ ref('int_rail_entrance_daily') }}
    where not is_line_level
), station_attributes as (
    select
        station_code,
        first(station_name order by ride_date desc, source_row_id desc)
            filter (where station_name is not null) as station_name,
        first(district order by ride_date desc, source_row_id desc)
            filter (where district is not null) as district,
        min(ride_date) as first_seen_date,
        max(ride_date) as last_seen_date,
        cast(count(distinct line_name) as bigint) as line_count
    from station_rows
    group by station_code
), distinct_coordinate_pairs as (
    select distinct
        station_code, year(ride_date) as ride_year, longitude, latitude
    from station_rows
    where longitude is not null and latitude is not null
), latest_coordinate_pairs as (
    select station_code, longitude, latitude
    from distinct_coordinate_pairs
    qualify ride_year = max(ride_year) over (partition by station_code)
), station_coordinates as (
    select
        station_code,
        cast(avg(longitude) as double) as longitude,
        cast(avg(latitude) as double) as latitude
    from latest_coordinate_pairs
    group by station_code
)
select
    a.station_code,
    a.station_name,
    a.district,
    c.longitude,
    c.latitude,
    a.first_seen_date,
    a.last_seen_date,
    a.line_count,
    nullif(g.station_group, '') as station_group
from station_attributes as a
left join station_coordinates as c on a.station_code = c.station_code
left join {{ ref('station_group_map') }} as g on a.station_code = g.station_code
