{{ config(materialized='view') }}
select
    try_cast(transition_date as date) as ride_date,
    try_cast(transition_hour as smallint) as ride_hour,
    trim(line_name) as line_code,
    line as line_route,
    station_poi_desc_cd as station_name_raw,
    {{ normalize_station_key('station_poi_desc_cd') }} as station_key,
    transfer_type,
    product_kind,
    transaction_type_desc,
    town,
    try_cast(number_of_passage as bigint) as passage_count,
    try_cast(number_of_passenger as bigint) as passenger_count,
    source_month,
    resource_id
from {{ source('raw_hourly', 'rail_hourly') }}
where road_type = 'RAYLI'
