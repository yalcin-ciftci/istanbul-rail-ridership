{{ config(materialized='table') }}

with dates as (
    select cast(day as date) as ride_date
    from generate_series(
        date '{{ var("calendar_start_date", "2021-01-01") }}',
        date '{{ var("calendar_end_date", "2025-12-31") }}',
        interval 1 day
    ) as spine(day)
), sources as (
    select
        d.ride_date,
        string_agg(distinct e.source_url, ' | ' order by e.source_url) as source_urls
    from dates as d
    inner join {{ ref('calendar_events') }} as e
        on d.ride_date between e.start_date and e.end_date
    group by d.ride_date
), calendar as (
    select
        d.ride_date,
        cast(isodow(d.ride_date) as integer) as day_of_week_iso,
        isodow(d.ride_date) in (6, 7) as is_weekend,
        coalesce(list_contains(string_split(c.event_types, ','), 'public_holiday'), false)
            as is_public_holiday,
        coalesce(list_contains(string_split(c.event_types, ','), 'religious_holiday'), false)
            as is_religious_holiday,
        coalesce(c.is_holiday, false) as is_holiday_full_day,
        coalesce(c.is_half_day_holiday, false) as is_holiday_half_day,
        coalesce(c.is_covid_restriction, false) as is_restriction_full_day,
        coalesce(c.is_partial_covid_restriction, false) as is_restriction_partial,
        coalesce(c.has_special_event, false) as is_special_event,
        c.labels as labels_tr,
        s.source_urls
    from dates as d
    left join {{ ref('int_calendar_day') }} as c on d.ride_date = c.ride_date
    left join sources as s on d.ride_date = s.ride_date
)
select
    ride_date,
    day_of_week_iso,
    is_weekend,
    is_public_holiday,
    is_religious_holiday,
    is_holiday_full_day,
    is_holiday_half_day,
    is_restriction_full_day,
    is_restriction_partial,
    is_special_event,
    labels_tr,
    source_urls,
    not is_weekend and not is_holiday_full_day and not is_restriction_full_day
        as is_regular_workday
from calendar
