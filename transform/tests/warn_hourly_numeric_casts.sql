{{ config(severity='warn', fail_calc='coalesce(sum(failure_rows), 0)') }}
-- depends_on: {{ ref('stg_rail_hourly') }}
select count(*) as failure_rows
from {{ source('raw_hourly', 'rail_hourly') }}
where road_type = 'RAYLI' and (
    (number_of_passage is not null and try_cast(number_of_passage as bigint) is null)
    or (number_of_passenger is not null and try_cast(number_of_passenger as bigint) is null)
)
having count(*) > 0
