with repaired_coordinates as (
    select
        _id,
        resource_id,
        transaction_year,
        transaction_month,
        transaction_day,
        line,
        station_number,
        station_name,
        town,
        {{ repair_coordinate('longitude') }} as lon,
        {{ repair_coordinate('latitude') }} as lat,
        passage_cnt,
        passanger_cnt,
        source_last_modified,
        ingested_at
    from {{ source('raw', 'rail_station_daily') }}
)

select
    resource_id || ':' || cast(_id as varchar) as row_key,
    cast(_id as bigint) as source_row_id,
    cast(resource_id as varchar) as resource_id,
    make_date(
        cast(transaction_year as integer),
        cast(transaction_month as integer),
        cast(transaction_day as integer)
    ) as ride_date,
    {{ fix_turkish_mojibake('cast(line as varchar)') }} as line_name,
    nullif(cast(station_number as varchar), '') as station_code,
    nullif({{ fix_turkish_mojibake('cast(station_name as varchar)') }}, '') as station_name,
    nullif(cast(station_number as varchar), '') is null as is_line_level,
    {{ fix_turkish_mojibake("nullif(town, '')") }} as district,
    case when lon is null or lat is null then null else lon end as longitude,
    case when lon is null or lat is null then null else lat end as latitude,
    cast(passage_cnt as bigint) as journey_count,
    cast(passanger_cnt as bigint) as passenger_count,
    cast(source_last_modified as timestamp) as source_last_modified,
    cast(ingested_at as timestamptz) as ingested_at
from repaired_coordinates
