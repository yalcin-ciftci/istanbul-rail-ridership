{{ config(materialized='view') }}

with expanded as (
    select
        cast(days.calendar_date as date) as ride_date,
        events.event_type,
        events.label_tr,
        events.partial_day
    from {{ ref('calendar_events') }} as events
    cross join lateral generate_series(
        events.start_date, events.end_date, interval 1 day
    ) as days(calendar_date)
), daily as (
    select
        ride_date,
        string_agg(distinct event_type, ',' order by event_type) as event_types,
        string_agg(label_tr, ' | ' order by event_type, label_tr) as labels,
        bool_or(event_type in ('public_holiday', 'religious_holiday')
            and not partial_day) as is_holiday,
        bool_or(event_type in ('public_holiday', 'religious_holiday')
            and partial_day) as has_partial_holiday,
        bool_or(event_type = 'covid_restriction' and not partial_day) as is_covid_restriction,
        bool_or(event_type = 'covid_restriction' and partial_day) as has_partial_restriction,
        bool_or(event_type = 'special_event') as has_special_event
    from expanded
    group by ride_date
)
select
    ride_date,
    event_types,
    labels,
    is_holiday,
    has_partial_holiday and not is_holiday as is_half_day_holiday,
    is_covid_restriction,
    has_partial_restriction and not is_covid_restriction as is_partial_covid_restriction,
    has_special_event
from daily
