select ride_date
from {{ ref('dim_calendar_day') }}
where is_regular_workday != (
    day_of_week_iso between 1 and 5
    and not is_holiday_full_day and not is_restriction_full_day
)
or is_weekend != (day_of_week_iso in (6, 7))
or (is_holiday_full_day and is_holiday_half_day)
or (is_restriction_full_day and is_restriction_partial)
or (is_holiday_full_day and not (is_public_holiday or is_religious_holiday))
