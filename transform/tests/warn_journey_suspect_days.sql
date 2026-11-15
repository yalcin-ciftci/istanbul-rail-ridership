{{ config(severity='warn') }}

select ride_date, row_count, even_share, journey_passenger_ratio
from {{ ref('int_journey_quality_daily') }}
where journey_suspect
