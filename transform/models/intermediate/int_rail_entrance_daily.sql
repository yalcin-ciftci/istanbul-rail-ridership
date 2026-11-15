select
    e.row_key,
    e.source_row_id,
    e.resource_id,
    e.ride_date,
    e.line_name,
    e.station_code,
    e.station_name,
    e.is_line_level,
    e.district,
    e.longitude,
    e.latitude,
    e.journey_count,
    e.passenger_count,
    e.source_last_modified,
    e.ingested_at,
    q.journey_suspect,
    cast(case
        when q.journey_suspect then e.journey_count // 2
        else e.journey_count
    end as bigint) as journey_count_estimated
from {{ ref('int_rail_entrance_deduped') }} as e
inner join {{ ref('int_journey_quality_daily') }} as q on e.ride_date = q.ride_date
