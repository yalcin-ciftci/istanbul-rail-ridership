with original as (
    select count(*) as row_count from {{ ref('int_daily_volume_anomaly') }}
), annotated as (
    select count(*) as row_count from {{ ref('int_daily_volume_anomaly_calendar') }}
)
select original.row_count as original_rows, annotated.row_count as annotated_rows
from original
cross join annotated
where original.row_count <> annotated.row_count
