{{ config(severity='warn') }}

-- Include this source-calendar check in int_daily_volume_anomaly+ builds.
-- depends_on: {{ ref('int_daily_volume_anomaly') }}
with bounds as (
    select min(ride_date) as first_day, max(ride_date) as last_day
    from {{ ref('fct_line_daily') }}
), date_spine as (
    select cast(d.ride_date as date) as ride_date
    from bounds as b,
        lateral generate_series(b.first_day, b.last_day, interval 1 day) as d(ride_date)
)
select d.ride_date
from date_spine as d
where not exists (
    select 1
    from {{ ref('fct_line_daily') }} as f
    where f.ride_date = d.ride_date
)
