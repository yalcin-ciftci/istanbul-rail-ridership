select ride_date, count(*) as row_count
from {{ ref('int_calendar_day') }}
group by ride_date
having count(*) > 1
